#!/usr/bin/env python3


import os
import glob
import random
import pickle
import logging
import itertools
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool

import torch
import hydra
import wandb
from omegaconf import DictConfig

from main import setup_wandb
from domain import make_domain
from utility import GRUUtilityFunction, LengthUtilityFunction, TwoStageUtilityFunction
from util import get_device
from episode import ProofSearchEpisode
from search import SearcherAgent, SearcherResults, run_search_on_batch, load_search_model
from policy import ContrastivePolicy, RandomPolicy, DiversityPolicy, DiversityPolicyVerifier
from tactics import induce_tactics, rewrite_episode_using_tactics


logger = logging.getLogger(__name__)


MAX_NODES_LIMIT = 50000


def spawn_searcher(rank, iteration, domain, tactics, max_nodes, max_depth,
                   epsilon, model_type, model_path, seeds, device, on_policy=False):
    out_path = (f'rollouts/it{iteration}/searcher{rank}.pt'
                if rank is not None
                else None)

    m = load_search_model(model_type, model_path, device=device)

    if out_path is not None and os.path.exists(out_path):
        with open(out_path, 'rb') as f:
            return pickle.load(f)

    algorithm = ('policy-beam-search'
                 if model_type in ('diversity-policy', 'diversity-verifier', 'contrastive-policy', 'random-policy')
                 else 'best-first-search')
    algorithm = 'on-policy-sample' if on_policy else algorithm

    agent = SearcherAgent(make_domain(domain, tactics),
                          m, max_nodes, max_depth,
                          epsilon=epsilon,
                          algorithm=algorithm)

    episodes = agent.run_batch(seeds)

    if out_path is not None:
        os.makedirs(os.path.dirname(out_path), exist_ok=True)

        with open(out_path, 'wb') as f:
            pickle.dump(episodes, f)

    return episodes


class TrainerAgent:
    def __init__(self, config):
        self.batch_size = config.batch_size
        self.iterations = config.iterations
        self.n_searchers = config.n_searchers
        self.max_nodes = config.max_nodes
        self.max_depth = config.max_depth
        self.model_type = config.model.type
        self.algorithm = config.algorithm
        self.train_domains = config.train_domains
        self.passing_grade = config.get('passing_grade', 0.0)
        self.search_budget_multiplier = config.get('search_budget_multiplier', 10)
        self.adjust_search_budget_threshold = config.get('adjust_search_budget_threshold', 0.0)
        self.eval_domains = config.eval_domains
        self.accumulate = config.accumulate
        self.epsilon = config.epsilon
        self.config = config
        self.searcher_futures = []

    def start(self):
        'Reloads the last checkpoint.'

        if 'run_dir' in self.config:
            os.chdir(self.config.run_dir)

        last_checkpoint = None
        tactics = []

        for i in itertools.count(0):
            path_i = os.path.join(os.getcwd(), f'{i}.pt')

            if os.path.exists(path_i):
                last_checkpoint = path_i
            else:
                iteration = i
                break

        episodes, episodes_iteration = [], -1

        for it in range(iteration, -1, -1):
            episodes_path_i = os.path.join(os.getcwd(), f'episodes-{it}.pkl')
            if os.path.exists(episodes_path_i):
                with open(episodes_path_i, 'rb') as f:
                    episodes = pickle.load(f)
                episodes_iteration = i
                print('Loaded', len(episodes), 'episodes from iteration', it)
                break

        if not episodes:
            if not self.config.load_trajectories:
                print('Starting with no training episodes.')
            else:
                print('Loading past episodes.')
                file_pattern = os.path.join(self.config.load_trajectories, "episodes-*.pkl")
                matching_files = glob.glob(file_pattern)
                if len(matching_files) == 0:
                    print("No episodes found")
                else:
                    matching_files.sort(key=lambda x: int(x.split("-")[1].split(".")[0]))
                    with open(matching_files[-1], "rb") as f:
                        episodes = pickle.load(f)
                    print(f"Loaded data from {matching_files[-1]}")


        for it in range(iteration, -1, -1):
            tactics_path_i = os.path.join(os.getcwd(), f'tactics-{it}.pkl')
            if os.path.exists(tactics_path_i):
                with open(tactics_path_i, 'rb') as f:
                    tactics = pickle.load(f)
                print('Loaded tactics from iteration', it)
                break

        if not tactics:
            if not self.config.load_tactics_init:
                print('Starting with no tactics.')
            else:
                print('Loading past tactics.')
                file_pattern = os.path.join(self.config.load_tactics_init, "tactics-*.pkl")
                matching_files = glob.glob(file_pattern)
                if len(matching_files) == 0:
                    print(f"No tactics found at {self.config.load_tactics_init}")
                else:
                    matching_files.sort(key=lambda x: int(x.split("-")[1].split(".")[0]))
                    
                    with open(matching_files[-1], "rb") as f:
                        tactics = pickle.load(f)
                    print(f"Loaded tactics from {matching_files[-1]}")

        device = get_device(self.config.get('gpus') and self.config.gpus[-1])

        if last_checkpoint is not None:
            print('Loading', last_checkpoint)
            m = torch.load(last_checkpoint, map_location=device)
        else:
            if self.config.model.type == 'utility':
                m = GRUUtilityFunction(self.config.model)
            elif self.config.model.type == 'contrastive-policy':
                m = ContrastivePolicy(self.config.model)
            elif self.config.model.type == 'diversity-policy':
                m = DiversityPolicy(self.config.model)
            elif self.config.model.type == 'diversity-verifier':
                m = DiversityPolicyVerifier(self.config.model)
            elif self.config.model.type == 'random-policy':
                m = RandomPolicy()
            if self.config.load_policy is not None:
                t_m = torch.load(self.config.load_policy, map_location=device)
                m.load_state_dict(t_m.state_dict())
                # import pdb; pdb.set_trace();
                iter_n = self.config.load_policy.split("/")[-1].split(".")[0]
                tactics_path = "/".join(self.config.load_policy.split("/")[:-1]) + "/tactics-" + iter_n + ".pkl"
                with open(tactics_path, "rb") as f:
                    tactics = pickle.load(f)
        m = m.to(device)
        return m, iteration, last_checkpoint, episodes, tactics, episodes_iteration

    def get_train_domain(self, curriculum_steps: int):
        return self.train_domains[min(curriculum_steps,
                                      len(self.train_domains) - 1)]

    def _get_searcher_device(self, searcher_index: int):
        if 'gpus' in self.config:
            return self.config.gpus[:-1][searcher_index % (len(self.config.gpus) - 1)]
        return 'cpu'

    def learn(self, seeds, eval_seeds):
        m, iteration, last_checkpoint, episodes, tactics, ep_it = self.start()
        max_nodes = self.max_nodes
        curriculum_steps = 0
        last_train_success_rate = 0
        start_index = (0 if self.accumulate else len(episodes))

        if len(episodes) > 0 and ep_it < 0:
            # i.e we loaded episodes from a past run
            # do some additional training in advance
            steps = m.gradient_steps
            m.gradient_steps = (len(episodes) // m.batch_size)
            logs = m.fit(episodes)
            m.gradient_steps = steps

        for it in range(iteration, self.config.iterations):
            metrics = {}
            torch.cuda.empty_cache()
            with ProcessPoolExecutor() as executor:
                logger.info("### Iteration %d ###", it)

                if ep_it >= it:
                    logger.info('Loaded dumped episodes for this iteration.')
                    logger.info('%d total, %d successful',
                                len(episodes),
                                sum(1 for e in episodes if e.success))
                else:
                    # Spawn N searchers.
                    logger.info('Spawning searchers on %s, max_nodes = %d...',
                                self.get_train_domain(curriculum_steps), max_nodes)
                    # wandb.log({'max_nodes': max_nodes,
                    #            'curriculum_steps': curriculum_steps})
                    metrics['max_nodes'] = max_nodes
                    metrics['curriculum_steps'] = curriculum_steps
                    
                    is_on_policy = [i < int(self.config.on_policy_frac*self.n_searchers) 
                                    for i in range(self.n_searchers)]
                    for j in range(self.n_searchers):
                        params = {
                            'iteration': it,
                            'rank': j,
                            'domain': self.get_train_domain(curriculum_steps),
                            'tactics': tactics,
                            'max_nodes': max_nodes,
                            'max_depth': self.max_depth,
                            'epsilon': self.epsilon,
                            'model_type': self.model_type,
                            'model_path': last_checkpoint,
                            'seeds': random.choices(seeds, k=self.batch_size),
                            'device': self._get_searcher_device(j),
                            'on_policy': is_on_policy[j],
                        }
                        logging.info('Searcher parameters: %s', str(params))
                        self.searcher_futures.append(executor.submit(spawn_searcher,
                                                                     **params))
                    # Evaluate the current agent.

                    if it % self.config.eval_freq == 0:
                        if self.config.do_eval:
                            logger.info('Evaluating...')
                            success_rate = {}
                            for d in self.eval_domains:
                                if not os.path.exists(f'eval-episodes-{d}-{it}.pkl'):
                                    eval_results = run_search_on_batch(
                                        make_domain(d, tactics),
                                        eval_seeds,
                                        m,
                                        self.algorithm,
                                        self.max_nodes,
                                        self.max_depth,
                                        output_path=f'eval-episodes-{d}-{it}.pkl',
                                        debug=False,
                                        epsilon=0,
                                    )
                                    # wandb.log({f'success_rate_{d}': eval_results.success_rate()})
                                    success_rate[d] = eval_results.success_rate()
                                    metrics.update({f'success_rate_{d}': eval_results.success_rate()})
                                    logger.info('Evaluated %s: %f', d, eval_results.success_rate())
                        if self.config.do_on_policy_eval:
                            logger.info('Evaluating on-policy...')
                            on_policy_success_rate = {}
                            for d in self.eval_domains:
                                if not os.path.exists(f'on-policy-episodes-{d}-{it}.pkl'):
                                    eval_results = run_search_on_batch(
                                        make_domain(d, tactics),
                                        eval_seeds,
                                        m,
                                        self.algorithm,
                                        self.max_nodes,
                                        self.max_depth,
                                        output_path=f'on-policy-eval-episodes-{d}-{it}.pkl',
                                        debug=False,
                                        epsilon=0,
                                        on_policy=True
                                    )
                                    on_policy_success_rate[d] = eval_results.success_rate()
                                    metrics.update({f'on_policy_success_rate_{d}': eval_results.success_rate()})
                                    logger.info('Evaluated %s: %f', d, eval_results.success_rate())

                    existing_episodes = len(episodes)
                    # beams = []
                    # Aggregate episodes from searchers.
                    on_policy_success = 0
                    on_policy_eps = 0
                    for i, f in enumerate(self.searcher_futures):
                        logger.info('Waiting for searcher #%d...', i)
                        try:
                            result_i = f.result()
                        except BrokenProcessPool as e:
                            logger.warning('Searcher #%d failed: %s. Ignoring results...',
                                           i, str(e))
                            continue

                        for j in range(len(result_i.episodes)):
                            d = make_domain(result_i.episodes[j].domain, tactics)
                            result_i.episodes[j] = rewrite_episode_using_tactics(
                                result_i.episodes[j], d, tactics)
                            if is_on_policy[i]:
                                on_policy_success += result_i.episodes[j].success
                                on_policy_eps += 1

                        episodes.extend(result_i.episodes)

                        # beams.extend(result_i.beams)
                    if sum(is_on_policy) > 0:
                        print(f'On policy success rate: {on_policy_success / on_policy_eps}')
                        metrics['on_policy_success_rate'] = on_policy_success / on_policy_eps
                        # wandb.log({'on_policy_success_rate': on_policy_success / on_policy_eps})

                    # Index of the first episode to use for training.
                    start_index = (0 if self.accumulate else existing_episodes)

                    # Induce tactics from new episodes.
                    if self.config.get('induce_tactics'):
                        if self.config.load_tactics_seq:
                            filepattern = os.path.join(self.config.load_tactics_seq, f"tactics-{it}.pkl")
                            matching_files = glob.glob(filepattern)
                            if len(matching_files) == 0:
                                print(f"No tactics found at {self.config.load_tactics_seq}")
                            else:
                                matching_files.sort(key=lambda x: int(x.split("-")[1].split(".")[0]))
                                with open(matching_files[-1], "rb") as f:
                                    tactics = pickle.load(f)
                                print(f"Loaded tactics from {matching_files[-1]}")
                                for new_tactic in tactics:
                                    for i, e in enumerate(episodes):
                                        d = make_domain(e.domain, tactics)
                                        episodes[i] = new_tactic.rewrite_episode(e, d)
                        else:
                            for _ in range(self.config.n_tactics):
                                proposals = induce_tactics(episodes[start_index:],
                                                        self.config.n_tactics,
                                                        self.config.min_tactic_score,
                                                        tactics,
                                                        self.config.get('induce_loops'))
                                if proposals:
                                    # Take the top proposal, incorporate it and repeat.
                                    new_tactic = (proposals[0]
                                                .rename(f'tactic{len(tactics):03d}'))

                                    logging.info('Incorporating new tactic:\n%s\n', new_tactic)
                                    tactics.append(new_tactic)

                                    for i, e in enumerate(episodes):
                                        d = make_domain(e.domain, tactics)
                                        episodes[i] = new_tactic.rewrite_episode(e, d)
                                else:
                                    break

                        logging.info('Recomputing negatives after tactic induction...')
                        for e in episodes:
                            if e.success:
                                d = make_domain(e.domain, tactics)
                                e.recompute_negatives(d)
                        logging.info('Done.')

                    self.searcher_futures = []

                    with open(f'episodes-{it}.pkl', 'wb') as f:
                        pickle.dump(episodes[start_index:], f)

                    with open(f'tactics-{it}.pkl', 'wb') as f:
                        pickle.dump(tactics, f)

                    train_success_rate = (sum(e.success 
                                              for e in episodes[existing_episodes:]) / 
                                          (max(1, len(episodes) - existing_episodes)))
                    print(f'Train success rate: {train_success_rate}')
                    metrics['train_success_rate'] = train_success_rate
                    # wandb.log({'train_success_rate': train_success_rate})

                    if train_success_rate >= self.passing_grade:
                        curriculum_steps += 1
                        logging.info('Success rate above passing grade, '
                                     'advancing in curriculum.')

                    if train_success_rate < 1 and \
                            (train_success_rate - last_train_success_rate) < \
                                self.adjust_search_budget_threshold:
                        max_nodes *= self.search_budget_multiplier
                        max_nodes = min(max_nodes, MAX_NODES_LIMIT)
                        logging.info('Train success rate too low (%f), '
                                     'increasing max search nodes to %d',
                                     train_success_rate, max_nodes)
                    else:
                        max_nodes = self.max_nodes

                    last_train_success_rate = train_success_rate
                torch.cuda.empty_cache()
                # Fit model and update checkpoint.
                logger.info('Training model on %d episodes (%d successful)',
                            len(episodes) - start_index,
                            sum(1 for e in episodes[start_index:] if e.success))
                logs = m.fit(episodes[start_index:])
                last_checkpoint = os.path.join(os.getcwd(), f'{it}.pt')
                metrics.update({k: v for k, v in logs.items()})
                print(metrics)
                wandb.log(metrics)
                torch.save(m, last_checkpoint)
                logger.info('Wrote %s', last_checkpoint)


@hydra.main(version_base="1.2", config_path="config", config_name="trainer")
def main(cfg: DictConfig):
    torch.multiprocessing.set_start_method('spawn')

    if cfg.task == 'train':
        setup_wandb(cfg)

        seeds = range(*cfg.train_interval)
        eval_seeds = range(*cfg.eval_interval)

        trainer = TrainerAgent(cfg.trainer)
        trainer.learn(seeds, eval_seeds)

    elif cfg.task == 'test':
        tactics = []

        if cfg.get('tactics'):
            with open(cfg.tactics, 'rb') as f:
                tactics = pickle.load(f)

        episodes = spawn_searcher(None, None,
                                  cfg.domain,
                                  tactics,
                                  cfg.searcher.max_nodes,
                                  cfg.searcher.max_depth,
                                  cfg.searcher.epsilon,
                                  cfg.searcher.model_type,
                                  cfg.searcher.model_path,
                                  range(*cfg.searcher.seed_interval),
                                  torch.device(cfg.searcher.device))

        with open(cfg.output, 'wb') as f:
            pickle.dump(episodes, f)


if __name__ == '__main__':
    main()
