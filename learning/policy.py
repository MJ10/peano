#!/usr/bin/env python3

import collections
from dataclasses import dataclass, field
from typing import Any, Optional
import logging
import random
from domain import Domain, EquationsDomain, Problem, make_domain
from enum import Enum
import unittest

import torch
from torch import nn
import torch.nn.functional as F
from torch.distributions.categorical import Categorical
import wandb
from tqdm import tqdm
import numpy as np
import math

from environment import Universe
from util import log, softmax, pop_max, batch_strings, encode_batch, decode_batch, PAD, EOS, BOS, POSITIVE, NEGATIVE, EMPTY
from solution import Solution, Action


logger = logging.getLogger(__name__)


# Left-truncate states to have at most this number of characters.
# This is to avoid overly large inputs to the state embedding model.
MAX_STATE_LENGTH = 200


@dataclass
class SearchNode:
    universe: Universe = None
    state: str = None
    value: float = 0.0
    parent: 'SearchNode' = None
    action: str = None
    outcome: str = None
    reward: bool = False
    value_target: float = None
    depth: int = 0

    def expand(self, domain: Domain) -> list['SearchNode']:
        c = []

        if self.action:
            # Expand outcomes.
            for o in self.universe.apply(self.action):
                u = make_updated_universe(self.universe, o, f'!subd{self.depth}')
                c.append(SearchNode(u, domain.state(u),
                                    depth=self.depth + 1,
                                    value=None, parent=self,
                                    action=None, outcome=o.clean_str(self.universe),
                                    reward=domain.reward(u)))
        else:
            # Expand actions.
            for a in domain.actions(self.universe):
                c.append(SearchNode(self.universe, f'S: {self.state} A: {a}', depth=self.depth,
                                    value=None, parent=self, action=a, reward=self.reward))

        return c

    def __getstate__(self):
        'Prevent pickle from trying to save the universe.'
        d = self.__dict__.copy()
        del d['universe']
        return d

    def __setstate__(self, d):
        self.__dict__.update(d)


@dataclass
class Episode:
    problem: str
    goal: str = None
    domain: str = None
    success: bool = False
    actions: list[str] = field(default_factory=list)
    arguments: list[str] = field(default_factory=list)
    states: list[str] = field(default_factory=list)
    # FIXME: this is obsolete, and now re-computed right before training.
    # Kept here for now to keep pickle-compatibility.
    negative_actions: list[list[str]] = field(default_factory=list)
    searched_nodes: list[SearchNode] = None

    def cleanup(self, domain: Domain):
        if not self.success:
            return

        actions = []

        # The very last action is always included, since we detected a solution right after it.
        actions = self.actions[-2:]
        negative_actions = self.negative_actions[-2:]

        for i in range(len(self.actions) - 4, -1, -2):
            # Try to ignore the i-th action and see if it affects anything that comes after it.
            action, outcome = self.actions[i], self.actions[i + 1]
            works = True

            if outcome == '_':
                # Ignore.
                continue

            # Add all actions up to the i-th, and then only those in `actions`.
            u = domain.start_derivation(self.problem, self.goal).universe

            ablated_solution = self.actions[:i] + actions

            for j in range(0, len(ablated_solution), 2):
                arrow, result = ablated_solution[j:j+2]
                outcomes = domain.apply(arrow, u)
                found = False

                for o in outcomes:
                    if domain.value_of(u, o) == result:
                        found = True
                        domain.define(u, f'!step{j // 2}', o)
                        break

                if not found:
                    works = False
                    break

            if not works:
                # Solution could not be replayed without the i-th action, so it's needed.
                actions = [action, outcome] + actions
                negative_actions = self.negative_actions[i:i+2] + negative_actions

        self.actions = actions
        self.states = Solution.states_from_episode(self.problem, self.goal, actions)
        self.negative_actions = negative_actions

        self.recover_arguments(domain)

    def recover_arguments(self, domain: 'Domain'):
        problem = domain.start_derivation(self.problem, self.goal)
        arguments = []

        for i, (arrow, outcome) in enumerate(zip(self.actions[::2], self.actions[1::2])):
            arguments.append([])

            if outcome == '_':
                arguments.append([])
                continue

            choices = domain.apply(arrow, problem.universe)
            definitions = [d for d in choices if domain.value_of(problem.universe, d) == outcome]

            if len(definitions) == 0:
                breakpoint()

            assert len(definitions) > 0, "Failed to replay the solution."

            arguments.append(definitions[0].generating_arguments())

            domain.define(problem.universe, f'!step{i}', definitions[0])

        self.arguments = arguments

    def recompute_negatives(self, domain: 'Domain'):
        problem = domain.start_derivation(self.problem, self.goal)
        solution = Solution.from_problem(problem)
        negatives = []

        for a in self.actions:
            successors = solution.successors(domain)
            positive_a, negatives_a = None, []

            for s in successors:
                if s.value == a:
                    positive_a = s
                else:
                    negatives_a.append(s.value)

            negatives.append(negatives_a)
            if positive_a is None:
                breakpoint()
            solution = solution.push_action(positive_a, domain)

        self.negative_actions = negatives

class MCTSNode:
    def __init__(self, state, prior: float = 0, exploration_constant: float = 1.0):
        self.state = state
        self.prior = prior
        self.visit_count = 0
        self.value_sum = 0
        self.children = {}  # action -> MCTSNode
        self.is_terminal = False
        self.exploration_constant = exploration_constant
        
    def value(self) -> float:
        if self.visit_count == 0:
            return 0
        return self.value_sum / self.visit_count
    
    def ucb_score(self, parent_visit_count: int) -> float:
        if self.visit_count == 0:
            return float('inf')
        # UCB formula: Q + c * P * sqrt(N) / (1 + n)
        exploitation = self.value()
        exploration = self.exploration_constant * self.prior * math.sqrt(parent_visit_count) / (1 + self.visit_count)
        return exploitation + exploration

def select(node: MCTSNode) -> tuple[list[tuple[MCTSNode, str]], MCTSNode]:
    path = []
    while node.children and not node.is_terminal:
        if len(node.children) == 0:
            break
            
        # Select action with highest UCB score
        best_action = max(node.children.items(),
                        key=lambda item: item[1].ucb_score(node.visit_count))[0]
        path.append((node, best_action))
        node = node.children[best_action]
        
    return path, node

def expand(node: MCTSNode, domain: Domain, policy, temperature):
    if node.is_terminal:
        return
    
    actions = node.state.solution.successors(domain)
    if len(actions) == 0:
        node.is_terminal = True
        return
    
    # Get action probabilities from policy network
    action_probs = (policy.score_arrows([a.value for a in actions], node.state.state) / 
                    temperature).softmax(-1)
    
    # Create child nodes for each action
    for action, prob in zip(actions, action_probs):
        if action.value not in node.children:
            new_state = BeamElement(
                solution=node.state.solution,
                state=node.state.state,
                action=action,
                logprob=node.state.logprob,
                parent=node.state,
                negative_actions=[a.value for a in actions if a != action]
            )
            new_state.solution = new_state.solution.push_action(action, domain)
            new_state.state = new_state.solution.format(MAX_STATE_LENGTH)
            node.children[action.value] = MCTSNode(new_state, prob.item(), exploration_constant=node.exploration_constant)

def backpropagate(path: list[tuple[MCTSNode, str]], leaf_value: float) -> None:
    for node, _ in path:
        node.value_sum += leaf_value
        node.visit_count += 1


@dataclass
class TreeSearchEpisode:
    initial_observation: str
    success: bool = False
    visited: list[SearchNode] = field(default_factory=list)
    goal_state: Optional[SearchNode] = None


@dataclass
class BeamElement:
    solution: Solution
    state: str
    action: Optional[Action] = None
    parent: Optional['BeamElement'] = None
    logprob: float = 0
    negative_actions: list = field(default_factory=list)

    def __str__(self) -> str:
        return f'BeamElement({self.state}, logprob={self.logprob})'


EMPTY_OUTCOME_PROBABILITY = 1e-3

def make_updated_universe(universe, definition, name):
    'Returns a clone of `universe` where `definition` is defined with the given name.'
    if definition == EMPTY:
        return universe
    u = universe.clone()
    u.define(name, definition)
    return u

def recover_episode(problem, final_state: BeamElement, success) -> Episode:
    states, actions, arguments, negative_actions, negative_outcomes = [], [], [], [], []

    current = final_state

    while current is not None:
        states.append(current.state)

        # The first action is associated with the second state, so for the
        # first state there's no action preceding it. Thus, `states` is one element
        # larger than the other lists.
        if current.parent is not None:
            actions.append(current.action.value)
            arguments.append(current.action.arguments)
            negative_actions.append(current.negative_actions)

        current = current.parent

    e = Episode(problem.description,
                problem.goal,
                problem.domain_name(),
                success,
                actions[::-1],
                arguments[::-1],
                states[::-1],
                negative_actions[::-1])
    e.cleanup(problem.domain)
    return e


class Policy(nn.Module):
    def __init__(self):
        super().__init__()

    def score_arrows(self, arrows: list[str], state: Any) -> torch.Tensor:
        'Scores the arrows that can be called.'
        raise NotImplementedError()

    def score_outcomes(self, outcomes: list[str], state: Any) -> torch.Tensor:
        'Scores the results that were produced by a given arrow.'
        raise NotImplementedError()

    def initial_state(self, observation: str) -> Any:
        'Returns the initial hidden state of the policy given the starting observation.'
        raise NotImplementedError()

    def next_state(self, state: Any, observation: str) -> Any:
        'Implements the recurrent rule to update the hidden state.'
        raise NotImplementedError()
    
    def on_policy_sample(self,
                         problem: Problem,
                         depth: int,
                         temperature: float = 1,
                         epsilon: float = 0) -> Episode:
        # raise NotImplementedError()
        with torch.no_grad():
            initial_sol = Solution.from_problem(problem)
            beam = BeamElement(solution=initial_sol,
                                state=initial_sol.format(MAX_STATE_LENGTH),
                                logprob=0.0)
            for it in range(depth + 1):
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug(f'Beam #{it}:')
                    logger.debug('  %s', beam)
                done_solution = problem.domain.derivation_done(beam.solution.derivation)
                
                if done_solution:
                    logger.debug('Solution state: %s', done_solution)
                    return recover_episode(problem, beam, True)
                
                if it == depth:
                    break
                
                take_random_action = random.random() < epsilon
                actions = beam.solution.successors(problem.domain)
                if len(actions) == 0:
                    return recover_episode(problem, beam, False)
                action_probs = (self.score_arrows([a.value for a in actions], beam.state) /
                                 temperature).softmax(-1)

                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug('Arrow probabilities:')
                    logger.debug('  %s => %s', beam.state,
                                 sorted(list(zip(actions, action_probs)),
                                        key=lambda aap: aap[1], reverse=True))
                if take_random_action:
                    action = random.sample(actions, 1)[0]
                else:
                    if len(action_probs) == 1:
                        action = actions[0]
                    else:
                        action = actions[torch.multinomial(action_probs, 1).item()]
                
                beam = BeamElement(solution=beam.solution,
                                      state=beam.state,
                                      action=action,
                                      logprob=beam.logprob + log(action_probs[actions.index(action)].item()),
                                      parent=beam,
                                      negative_actions=[a.value for a in actions if a != action])
                                   
                beam.solution = beam.solution.push_action(action, problem.domain)
                beam.state = beam.solution.format(MAX_STATE_LENGTH)
            torch.cuda.empty_cache()
            return recover_episode(problem, beam, False)

    def beam_search(self,
                    problem: Problem,
                    depth: int,
                    temperature: float = 1,
                    beam_size: int = 1,
                    epsilon: float = 0) -> Episode:

        with torch.no_grad():
            initial_sol = Solution.from_problem(problem)
            beam = [BeamElement(solution=initial_sol,
                                state=initial_sol.format(MAX_STATE_LENGTH),
                                logprob=0.0)]

            # Each iteration happens in 3 stages:
            # 0- Check if a solution was found
            # 1- Score actions for each state in the beam
            # 2- Rerank and apply outcome to states to obtain next beam
            for it in range(depth + 1):
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug(f'Beam #{it}:')
                    for e in beam:
                        logger.debug('  %s', e)

                # 0- Check if a solution was found
                done_solution = next((s for s in beam
                                      if problem.domain.derivation_done(s.solution.derivation)), None)

                if done_solution is not None:
                    logger.debug('Solution state: %s', done_solution)
                    return recover_episode(problem, done_solution, True)# , done_solution

                if it == depth:
                    # On the extra iteration, just check if we have a solution, but otherwise
                    # don't keep expanding nodes.
                    break

                # epsilon-greedy: pick random actions in this iteration with probability eps.
                take_random_action = random.random() < epsilon

                # 1- Expand each node in the beam and score successors.
                actions = [s.solution.successors(problem.domain) for s in beam]
                # if hasattr(self, 'score_arrows_batch'):
                #     action_probs = self.score_arrows_batch([[a.value for a in a_i] for a_i in actions],
                #                                            [s.state for s in beam]).softmax(-1)
                # else:
                action_probs = [(self.score_arrows([a.value for a in a_i],
                                                s.state) / temperature).softmax(-1)
                            for a_i, s in zip(actions, beam)]

                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug('Arrow probabilities:')
                    for i, s in enumerate(beam):
                        logger.debug('  %s => %s', s.state,
                                     sorted(list(zip(actions[i], action_probs[i])),
                                            key=lambda aap: aap[1], reverse=True))

                beam = [BeamElement(solution=s.solution,
                                    state=s.state,
                                    action=a,
                                    logprob=s.logprob + log(action_probs[i][j].item()),
                                    parent=s,
                                    negative_actions=[a.value
                                                      for a in actions[i][:j] + actions[i][j+1:]])
                        for i, s in enumerate(beam)
                        for j, a in enumerate(actions[i])]

                # 2- Compute next beam
                if take_random_action:
                    beam = random.sample(beam, k=min(len(beam), beam_size))
                else:
                    beam = sorted(beam, key=lambda s: -s.logprob)[:beam_size]

                for e in beam:
                    e.solution = e.solution.push_action(e.action, problem.domain)
                    e.state = e.solution.format(MAX_STATE_LENGTH)
            torch.cuda.empty_cache()
            return recover_episode(problem, beam[0] if beam else None, False)# , beam[0] if beam else None

    def best_first_search(self, domain: Domain, problem: Problem,
                          max_nodes: int) -> Episode:
        root = SearchNode(problem.universe,
                          domain.state(problem.universe),
                          reward=domain.reward(problem.universe))
        with torch.no_grad():
            root.value = self.estimate_values([root.state]).item()
        queue = [root]
        visited = []
        goal_state = root if root.reward else None

        while queue and goal_state is None and len(visited) < max_nodes:
            node, queue = pop_max(queue, lambda node: node.value)
            visited.append(node)

            logger.debug('Visiting %s (estimated value: %f)', node.state, node.value)

            children = node.expand(domain)

            if children:
                with torch.no_grad():
                    children_values = self.estimate_values([c.state for c in children])

                for c, v in zip(children, children_values):
                    c.value = v
                    logger.debug('\tEstimated value for children %s / %s: %f',
                                 c.state, c.action, c.value)

                    if c.reward:
                        goal_state = c

                queue.extend(children)

        # For all nodes that are not in the path to the solution, aim to reduce their
        # value estimates. This will happen to all nodes in case no solution is found
        # and the agent doesn't ignore unsolved problems.
        for node in visited:
            node.value_target = 0

        if goal_state:
            visited.append(goal_state)
            node = goal_state
            value = 1.0
            while node is not None:
                node.value_target = value
                value *= 0.98
                node = node.parent

        return TreeSearchEpisode(problem.description,
                                 goal_state is not None,
                                 visited,
                                 goal_state)

    def extract_examples(self, episode) -> list[str]:
        raise NotImplementedError()

    def get_loss(self, batch) -> torch.Tensor:
        raise NotImplementedError()

    def embed_raw(self, strs: list[str]) -> torch.Tensor:
        raise NotImplementedError()

    def embed_states(self, batch: list[str]) -> torch.Tensor:
        return self.embed_raw([f'S{s}S' for s in batch])

    def embed_arrows(self, batch: list[str]) -> torch.Tensor:
        return self.embed_raw([f'A{s}A' for s in batch])

    def embed_outcomes(self, batch: list[str]) -> torch.Tensor:
        batch = batch or [EMPTY]
        return self.embed_raw([f'O{s}O' for s in batch])

    def mcts_search(self,
                    problem: Problem,
                    num_simulations: int = 100,
                    max_depth: int = 50,
                    exploration_constant: float = 1.0,
                    temperature: float = 1.0) -> Episode:
        """
        Performs Monte Carlo Tree Search using the policy network for action selection and value estimation.
        
        Args:
            problem: The problem to solve
            num_simulations: Number of MCTS simulations to run
            max_depth: Maximum depth of the search tree
            exploration_constant: Controls exploration in UCB formula
            temperature: Temperature for policy sampling
        """

        # Initialize root node
        initial_sol = Solution.from_problem(problem)
        root = MCTSNode(BeamElement(solution=initial_sol,
                                   state=initial_sol.format(MAX_STATE_LENGTH),
                                   logprob=0.0), exploration_constant=exploration_constant)

        # Run MCTS simulations
        with torch.no_grad():
            for _ in range(num_simulations):
                path, leaf = select(root)
                
                # Check if we've reached a terminal state or max depth
                if len(path) >= max_depth:
                    leaf.is_terminal = True
                
                if not leaf.is_terminal:
                    expand(leaf, problem.domain, self, temperature)
                    
                # Check if solution is found
                if problem.domain.derivation_done(leaf.state.solution.derivation):
                    leaf.is_terminal = True
                    leaf_value = 1.0
                else:
                    # Use value network to estimate state value
                    leaf_value = self.estimate_values([leaf.state.state]).item()
                    
                backpropagate(path, leaf_value)

        # Select best action sequence from root
        current = root
        best_episode = None
        while current.children and not current.is_terminal:
            # Select action with highest visit count
            best_action = max(current.children.items(),
                             key=lambda item: item[1].visit_count)[0]
            current = current.children[best_action]
            
            # Check if we found a solution
            if problem.domain.derivation_done(current.state.solution.derivation):
                best_episode = recover_episode(problem, current.state, True)
                break

        if best_episode is None:
            best_episode = recover_episode(problem, current.state, False)

        return best_episode


class RNNObservationEmbedding(nn.Module):
    def __init__(self, config):
        super().__init__()
        embedding_size = 16
        hidden_size = 32
        layers = 1

        self.rnn = nn.GRU(embedding_size, hidden_size // 2, layers, bidirectional=True)
        self.char_embedding = nn.Embedding(128, embedding_size)

    def forward(self, observations: list[str]) -> torch.Tensor:
        o = encode_batch(observations, self.char_embedding.weight.device)
        o = o.transpose(0, 1)
        o_char_emb = self.char_embedding(o)
        out, h_n = self.rnn(o_char_emb)
        return h_n[-2:, :, :].transpose(0, 1).reshape((len(observations), -1))


class GRUPolicy(Policy):
    def __init__(self, config, all_arrows):
        super().__init__()
        hidden_size = 32

        self.rnn_cell = nn.GRUCell(hidden_size, hidden_size)

        self.arrow_readout = nn.Linear(hidden_size, hidden_size)
        self.outcome_readout = nn.Linear(hidden_size, hidden_size)

        self.embedding = RNNObservationEmbedding({})
        self.arrow_embedding = nn.Embedding(len(all_arrows), hidden_size)
        self.arrow_to_index = {a: i for i, a in enumerate(all_arrows)}

    def initial_state(self, observation: str) -> torch.Tensor:
        return self.embedding([observation])[0]

    def next_state(self, state: torch.Tensor, observation: str):
        obs_emb = self.embedding([observation])[0]
        return self.rnn_cell(obs_emb, state)

    def score_arrows(self, arrows: list[str], state: torch.Tensor) -> torch.Tensor:
        arrow_index = torch.LongTensor([self.arrow_to_index[a] for a in arrows],
                                       device=self.arrow_embedding.weight.device)
        arrow_embeddings = self.arrow_embedding(arrow_index)
        readout = self.arrow_readout(state)
        H = readout.shape[0]
        return arrow_embeddings.matmul(readout.unsqueeze(1)).squeeze(1) / H

    def score_outcomes(self, outcomes: list[str], state: torch.Tensor) -> torch.Tensor:
        outcome_embeddings = self.embedding(outcomes)
        readout = self.outcome_readout(state)
        H = readout.shape[0]
        return outcome_embeddings.matmul(readout.unsqueeze(1)).squeeze(1) / H


class RandomPolicy(Policy):
    def __init__(self, config=None):
        super().__init__()

    def initial_state(self, observation: str) -> torch.Tensor:
        return torch.tensor([])

    def next_state(self, state: torch.Tensor, observation: str):
        return state

    def score_arrows(self, arrows: list[str], state: torch.Tensor) -> torch.Tensor:
        return torch.rand((len(arrows),))

    def score_outcomes(self, outcomes: list[str], state: torch.Tensor, action: str, goal: str) -> torch.Tensor:
        return torch.rand((len(outcomes),))

    def fit(self, *args, **kwargs):
        pass

    def estimate_values(self, states: list[str]) -> torch.Tensor:
        return torch.zeros((len(states),))


class ConstantPolicy(Policy):
    'Used for debugging.'
    def __init__(self, arrow, config=None):
        super().__init__()
        self.arrows = arrow if isinstance(arrow, set) else set([arrow])

    def score_arrows(self, arrows: list[str], state: torch.Tensor) -> torch.Tensor:
        return torch.tensor([10000 * int(a in self.arrows) for a in arrows])

    def score_outcomes(self, outcomes: list[str], state: torch.Tensor, action: str, goal: str) -> torch.Tensor:
        return torch.rand((len(outcomes),))


class DecisionTransformer(Policy):
    def __init__(self, config):
        super().__init__()

        # configuration = ReformerConfig(
        #     vocab_size=128,
        #     attn_layers=['local', 'lsh'] * (config.reformer.num_hidden_layers // 2),
        #     #            axial_pos_shape=(32, 32), # Default (64, 64) -- must multiply to seq len when training
        #     # Default (64, 64) -- must multiply to seq len when training
        #     axial_pos_embds_dim=(64, config.reformer.hidden_size - 64),
        #     bos_token_id=BOS,
        #     eos_token_id=EOS,
        #     pad_token_id=PAD,
        #     is_decoder=True,
        #     **config['reformer']
        # )

        configuration = GPT2Config(
            vocab_size=128,
            bos_token_id=BOS,
            eos_token_id=EOS,
            pad_token_id=PAD,
            n_positions=2048)

        # Initializing a Reformer model
        self.lm = GPT2LMHeadModel(configuration) # ReformerModelWithLMHead(configuration)
        self.train_len_multiple = 64*64
        self.batch_size = 4000
        self.mask_non_decision_tokens = config.mask_non_decision_tokens
        self.max_negatives = config.max_negatives

    def initial_state(self, observation: str) -> torch.Tensor:
        raise NotImplementedError()
        return encode_batch([f'G (= x ?);S {observation}'],
                            self.lm.device,
                            eos=False)[0]

    def next_state(self, state: torch.Tensor, action: str, observation: str):
        raise NotImplementedError()
        return torch.cat((state,
                          encode_batch([f';A {action};O {observation}'],
                                       device=state.device, bos=False, eos=False)[0]))

    def score_arrows(self, arrows: list[str], state: str) -> torch.Tensor:
        return self._score_continuations(state, goal, '; A ', arrows)

    def score_outcomes(self, outcomes: list[str], action: str, state: str, goal: str) -> torch.Tensor:
        return self._score_continuations(state, goal, f'; A {action}; O ', outcomes)

    def _score_continuations(self,
                             state: str,
                             goal: str,
                             prefix: str,
                             continuations: list[str]) -> torch.Tensor:
        if not continuations:
            return torch.tensor([])

        state = encode_batch([f'S {state}; G {goal}'],
                             self.lm.device,
                             eos=False)[0]
        P = encode_batch([prefix for _ in continuations],
                         bos=False, eos=False, device=state.device)
        C = encode_batch(continuations,
                         bos=False, eos=False, device=state.device)

        input_ids = torch.cat((state.repeat((len(C), 1)), P, C), dim=1)

        # Run the LM on smaller batches if needed to avoid running it on
        # more than self.batch_size tokens at a time.
        outputs = []
        batch_rows = max(1, self.batch_size // input_ids.shape[1])

        for row in range((input_ids.shape[0] + batch_rows - 1) // batch_rows):
            i = row * batch_rows
            j = min(i + batch_rows, input_ids.shape[0])
            X = input_ids[i:j, :]
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug('Calling DecisionTransformer LM:')
                strs = decode_batch(X)
                for s in strs:
                    logger.debug('"%s"', s)
            outputs.append(self.lm(X, attention_mask=(X != PAD).float()).logits)

        output = torch.cat(outputs, dim=0)

        prediction = output.softmax(dim=-1)

        skip = state.shape[0] + P.shape[1]
        action_predictions = prediction[range(len(continuations)),
                                        [skip + len(c) - 1 for c in continuations],
                                        :]

        pos_logit = action_predictions[:, POSITIVE]
        neg_logit = action_predictions[:, NEGATIVE]
        scores = pos_logit - neg_logit

        logger.debug('{"location": "_score_continuations", "input_ids.shape": %s, "prefix": "%s",'
                     '"continuations": %s, "scores": %s}',
                     input_ids.shape, prefix, continuations, scores)

        return scores

    def pad_train_batch(self, tensor: torch.Tensor):
        return tensor
        m = self.train_len_multiple
        n = tensor.shape[-1]
        next_multiple_of_m = (n + m - 1) // m * m
        return F.pad(tensor, (0, next_multiple_of_m - n))

    def extract_examples(self, episode,
                         transform_state=lambda s: s,
                         transform_goal=lambda g: g) -> list[str]:
        if not episode.success:
            return []

        # Positive.
        def format_example(s, g, a, c):
            return f'S {transform_state(s)}; G {transform_goal(g)}; {a}{c}'

        examples = []

        for i, (a, o) in enumerate(episode.actions):
            # Negative examples of actions.
            examples.extend([format_example(episode.states[i],
                                            episode.goal,
                                            f'A {neg}', chr(NEGATIVE))
                             for neg in self._sample_negatives(episode.negative_actions[i])])

            # Negative examples of outcomes.
            examples.extend([format_example(episode.states[i],
                                            episode.goal,
                                            f'A {a}; O {neg}', chr(NEGATIVE))
                             for neg in self._sample_negatives(episode.negative_outcomes[i])])

            # Positives
            examples.append(format_example(episode.states[i],
                                           episode.goal,
                                           f'A {a}', chr(POSITIVE)))
            examples.append(format_example(episode.states[i],
                                           episode.goal,
                                           f'A {a}; O {o}', chr(POSITIVE)))

        return examples

    def _sample_negatives(self, negatives):
        return random.sample(negatives, k=min(len(negatives), self.max_negatives))

    def get_loss(self, batch) -> torch.Tensor:
        t = encode_batch(batch, self.lm.device)

        # NOTE: The Reformer implementation already shifts X and y.
        # Normally, we'd have to do this manually.
        X = self.pad_train_batch(t)
        y = X.clone()

        # Ignore non-decision tokens (or at least PAD tokens) when computing the loss
        # (-100 is the label mask ID from the huggingface API).
        if self.mask_non_decision_tokens:
            y[(y != POSITIVE) & (y != NEGATIVE)] = -100
        else:
            y[y == PAD] = -100

        output = self.lm(X,
                         attention_mask=(X != PAD).float(),
                         labels=y)

        return output.loss


class DecisionGRU(Policy):
    def __init__(self, config):
        super().__init__()

        self.lm = nn.GRU(input_size=config.gru.embedding_size,
                         hidden_size=config.gru.hidden_size,
                         num_layers=config.gru.layers)

        self.output = nn.Linear(config.gru.hidden_size, 128)
        self.embedding = nn.Embedding(128, config.gru.embedding_size)
        self.batch_size = config.batch_size
        self.mask_non_decision_tokens = False

    def score_arrows(self, arrows: list[str], state: str) -> torch.Tensor:
        return self._score_continuations(state, ';A ', arrows)

    def score_outcomes(self, outcomes: list[str], action: str, state: str) -> torch.Tensor:
        return self._score_continuations(state, f';A {action};O ', outcomes)

    def get_device(self):
        return self.embedding.weight.device

    def _score_continuations(self,
                             state: str,
                             prefix: str,
                             continuations: list[str]) -> torch.Tensor:
        if not continuations:
            return torch.tensor([])

        S = encode_batch([f'S {state}'],
                         self.get_device(),
                         eos=False)[0]
        P = encode_batch([prefix for _ in continuations],
                         bos=False, eos=False, device=S.device)
        C = encode_batch(continuations,
                         bos=False, eos=False, device=S.device)

        input_ids = torch.cat((S.repeat((len(C), 1)), P, C), dim=1)

        # Run the LM on smaller batches if needed to avoid running it on
        # more than self.batch_size tokens at a time.
        outputs = []
        batch_rows = max(1, self.batch_size // input_ids.shape[1])

        for row in range((input_ids.shape[0] + batch_rows - 1) // batch_rows):
            i = row * batch_rows
            j = min(i + batch_rows, input_ids.shape[0])
            X = self.embedding(input_ids[i:j, :].transpose(0, 1))
            y, _ = self.lm(X)
            outputs.append(self.output(y.transpose(0, 1)))

        output = torch.cat(outputs, dim=0)

        prediction = output.softmax(dim=-1)

        skip = S.shape[0] + P.shape[1]
        action_predictions = prediction[range(len(continuations)),
                                        [skip + len(c) - 1 for c in continuations],
                                        :]

        pos_logit = action_predictions[:, POSITIVE]
        neg_logit = action_predictions[:, NEGATIVE]
        scores = pos_logit - neg_logit

        logger.debug('{"location": "_score_continuations", "input_ids.shape": %s, "prefix": "%s",'
                     '"continuations": %s, "scores": %s}',
                     input_ids.shape, prefix, continuations, scores)

        return scores

    def extract_examples(self, episode) -> list[str]:
        if not episode.success:
            return []

        # Positive.
        def format_example(s, a, c):
            return f'S {s}; {a}{c}'

        examples = []

        for i, (a, o) in enumerate(episode.actions):
            # Negative examples of actions.
            examples.extend([format_example(episode.states[i],
                                            f'A {neg}', chr(NEGATIVE))
                             for neg in episode.negative_actions[i]])

            # Negative examples of outcomes.
            examples.extend([format_example(episode.states[i],
                                            f'A {a}; O {neg}', chr(NEGATIVE))
                             for neg in episode.negative_outcomes[i]])

            # Positives
            examples.append(format_example(episode.states[i],
                                           f'A {a}', chr(POSITIVE)))
            examples.append(format_example(episode.states[i],
                                           f'A {a}; O {o}', chr(POSITIVE)))

        return examples

    def get_loss(self, batch) -> torch.Tensor:
        t = encode_batch(batch, self.get_device()).transpose(0, 1)

        X = self.embedding(t[:-1, :])
        y = t[1:, :].clone()

        # Ignore non-decision tokens (or at least PAD tokens) when computing the loss
        # (-100 is the label mask ID for cross_entropy_loss).
        if self.mask_non_decision_tokens:
            y[(y != POSITIVE) & (y != NEGATIVE)] = -100
        else:
            y[y == PAD] = -100

        y_hat, _ = self.lm(X)
        output = self.output(y_hat)

        # output shape is (L, N, C), cross_entropy needs (N, C, L).
        return F.cross_entropy(output.permute((1, 2, 0)), y.transpose(0, 1))


class ExampleType(Enum):
    STATE_ACTION = 1
    STATE_OUTCOME = 2
    STATE_VALUE = 3


@dataclass
class ContrastivePolicyExample:
    type: ExampleType
    state: str
    positive: str = None
    negatives: list[str] = None
    value: float = None

    def __len__(self):
        return (len(self.state) +
                len(self.positive or '') + 
                sum(map(len, self.negatives or [])))


@dataclass
class DBExample:
    state: str
    actions: str = None
    next_state: str = None
    is_terminal: bool = False

class ContrastivePolicy(Policy):
    def __init__(self, config):
        super().__init__()

        self.lm = nn.GRU(input_size=config.gru.embedding_size,
                         hidden_size=config.gru.hidden_size,
                         bidirectional=True,
                         num_layers=config.gru.layers)

        self.arrow_readout = nn.Linear(2*config.gru.hidden_size, 2*config.gru.hidden_size)
        self.outcome_readout = nn.Linear(2*config.gru.hidden_size, 2*config.gru.hidden_size)
        self.value_readout = nn.Sequential(
            nn.Linear(2*config.gru.hidden_size, 2*config.gru.hidden_size),
            nn.ReLU(),
            nn.Linear(2*config.gru.hidden_size, 1)
        )

        self.embedding = nn.Embedding(128, config.gru.embedding_size)
        self.discard_unsolved = config.discard_unsolved
        self.train_value_function = config.train_value_function

        # Truncate states/actions to avoid OOMs.
        self.max_len = MAX_STATE_LENGTH
        self.discount = 0.99
        self.batch_size = config.batch_size
        self.lr = config.lr
        self.gradient_steps = config.gradient_steps
        self.solution_augmentation_probability = config.solution_augmentation_probability
        self.solution_augmentation_rate = config.solution_augmentation_rate


    def score_arrows(self, arrows: list[str], state: str) -> torch.Tensor:
        if len(arrows) <= 1:
            return torch.ones(len(arrows), dtype=torch.float, device=self.get_device())
        # state_embedding : (1, H)
        state_embedding = self.embed_states([state])
        # arrow_embedding : (B, H)
        arrow_embeddings = self.embed_arrows(arrows)
        # state_t : (H, 1)
        state_t = self.arrow_readout(state_embedding).transpose(0, 1)
        # Result: (B,)
        return arrow_embeddings.matmul(state_t).squeeze(1)

    def score_outcomes(self, outcomes: list[str], action: str, state: str) -> torch.Tensor:
        if len(outcomes) <= 1:
            return torch.ones(len(outcomes), dtype=torch.float, device=self.get_device())
        # state_embedding : (1, H)
        state_embedding = self.embed_states([state])
        # outcome_embeddings : (B, H)
        outcome_embeddings = self.embed_outcomes(outcomes)
        # state_t : (H, 1)
        state_t = self.outcome_readout(state_embedding).transpose(0, 1)
        # Result: (B,)
        return outcome_embeddings.matmul(state_t).squeeze(1)

    def estimate_values(self, states: list[str]) -> torch.Tensor:
        logger.debug('Estimating values for %d states, maxlen = %d',
                     len(states), max(map(len, states)))
        state_embedding = self.embed_states(states)
        return self.value_readout(state_embedding).squeeze(1)

    def get_device(self):
        return self.embedding.weight.device

    def extract_examples(self, episode, random_negatives=[]) -> list[ContrastivePolicyExample]:
        examples = []

        if not episode.success and self.discard_unsolved:
            return examples

        if isinstance(episode, Episode):
            for i, a in enumerate(episode.actions):
                if episode.success:
                    if episode.negative_actions[i]:
                        examples.append(ContrastivePolicyExample(type=ExampleType.STATE_ACTION,
                                                                 state=episode.states[i],
                                                                 positive=a,
                                                                 negatives=episode.negative_actions[i]))

                        if random_negatives and \
                           random.random() < self.solution_augmentation_probability:
                            examples.extend(self._perform_augmentation(episode, random_negatives))

            if self.train_value_function:
                for i, s in enumerate(episode.states):
                    value = (0 if not episode.success
                             else self.discount ** (len(episode.states) - (i + 1)))
                    examples.append(ContrastivePolicyExample(type=ExampleType.STATE_VALUE,
                                                             state=episode.states[i],
                                                             value=value))
        elif isinstance(episode, TreeSearchEpisode):
            for node in episode.visited:
                examples.append(ContrastivePolicyExample(type=ExampleType.STATE_VALUE,
                                                         state=node.state,
                                                         value=node.value_target))

        return examples

    def _perform_augmentation(self, episode, random_negatives):
        # Add a few random negatives to the solution
        augmented_actions = []

        # How many to insert.
        n_insertions = np.random.geometric(p=self.solution_augmentation_rate)
        # Where to insert random steps.
        queue = sorted(random.choices(list(range(len(episode.actions) // 2)),
                                      k=n_insertions), reverse=True)
        is_augmentation = []
        positives, negatives = [], []

        for i in range(len(episode.actions) // 2):
            while queue and queue[-1] == i:
                queue.pop()
                augmented_actions.extend(random.choice(random_negatives))

                if random.random() < 0.5:
                    augmented_actions[-1] = '_'

                positives.extend(episode.actions[2*i:2*i+2])
                negatives.extend(episode.negative_actions[2*i:2*i+2])
                is_augmentation.append(True)

            augmented_actions.extend(episode.actions[2*i:2*i+2])
            positives.extend(episode.actions[2*i:2*i+2])
            negatives.extend(episode.negative_actions[2*i:2*i+2])
            is_augmentation.append(False)

        augmentations = []

        states = Solution.states_from_episode(episode.problem, episode.goal,
                                              augmented_actions)

        assert len(states) == 1 + len(augmented_actions)

        for i in range(len(is_augmentation)):
            if i and is_augmentation[i-1]:
                augmentations.append(ContrastivePolicyExample(
                    type=ExampleType.STATE_ACTION,
                    state=states[2*i],
                    positive=positives[2*i],
                    negatives=negatives[2*i]
                ))

        return augmentations


    def get_loss(self, batch) -> torch.Tensor:
        losses = []
        # HACK: This can be vectorized & batched, but it will be more complicated because
        # the number of classes is different for each contrastive example in the batch.
        for e in batch:
            if e.type == ExampleType.STATE_ACTION:
                # import pdb; pdb.set_trace();
                p = self.score_arrows([e.positive] + e.negatives, e.state)
                losses.append(F.cross_entropy(p.unsqueeze(0), torch.zeros((1,), device=p.device, dtype=torch.long)))
            elif e.type != ExampleType.STATE_VALUE:
                raise ValueError(f'Unknown example type {e.type}')

        state_values_x = [e.state for e in batch if e.type == ExampleType.STATE_VALUE]

        if len(state_values_x):
            y = [e.value for e in batch if e.type == ExampleType.STATE_VALUE]
            y_hat = self.estimate_values(state_values_x)
            losses.append(((y_hat - torch.tensor(y, device=y_hat.device))**2).mean())

        return torch.stack(losses, dim=0).mean()

    def embed_raw(self, strs: list[str]) -> torch.Tensor:
        strs = [s[:self.max_len] for s in strs]
        outputs = []

        for b in batch_strings(strs, batch_size=4096):
            input = encode_batch(b, self.get_device(), bos=True, eos=True)
            input = self.embedding(input.transpose(0, 1))
            lm_output, _ = self.lm(input)
            outputs.append(lm_output[0, :, :])

        return torch.cat(outputs, dim=0)

    def fit(self,
            dataset: list[Episode],
            checkpoint_callback=lambda: None):
        self.train()

        optimizer = torch.optim.Adam(self.parameters(), lr=self.lr)

        all_negatives = []

        for e in dataset:
            for i in range(len(e.actions) // 2):
                all_negatives.append(e.actions[2*i:2*i+2])

        # Assemble contrastive examples
        examples = []

        # import pdb; pdb.set_trace();
        for episode in dataset:
            examples.extend(self.extract_examples(episode, all_negatives))
        
        losses = []
        for e in range(self.gradient_steps):
            optimizer.zero_grad()
            batch = random.sample(examples, k=min(len(examples), self.batch_size))
            loss = self.get_loss(batch)
            loss.backward()
            optimizer.step()
            losses.append(loss.item())
            # wandb.log({'train_loss': loss.cpu()})

            checkpoint_callback()
        return {
            'loss': np.mean(losses),
            # 'losses': losses
        }        


class DiversityPolicy(Policy):
    def __init__(self, config):
        super().__init__()

        self.lm = nn.GRU(input_size=config.gru.embedding_size,
                         hidden_size=config.gru.hidden_size,
                         bidirectional=True,
                         num_layers=config.gru.layers)

        self.arrow_readout = nn.Linear(2*config.gru.hidden_size, 2*config.gru.hidden_size)
        self.outcome_readout = nn.Linear(2*config.gru.hidden_size, 2*config.gru.hidden_size)
        self.value_readout = nn.Sequential(
            nn.Linear(2*config.gru.hidden_size, 2*config.gru.hidden_size),
            nn.ReLU(),
            nn.Linear(2*config.gru.hidden_size, 1)
        )

        self.embedding = nn.Embedding(128, config.gru.embedding_size)
        self.discard_unsolved = config.discard_unsolved
        self.train_value_function = config.train_value_function

        # Truncate states/actions to avoid OOMs.
        self.max_len = MAX_STATE_LENGTH
        self.discount = 0.99
        self.batch_size = config.batch_size
        self.lr = config.lr
        self.gradient_steps = config.gradient_steps
        self.solution_augmentation_probability = config.solution_augmentation_probability
        self.solution_augmentation_rate = config.solution_augmentation_rate
        self.positives_only = config.positives_only
        self.loss = config.loss


    def score_arrows(self, arrows: list[str], state: str) -> torch.Tensor:
        if len(arrows) <= 1:
            return torch.ones(len(arrows), dtype=torch.float, device=self.get_device())
        # state_embedding : (1, H)
        state_embedding = self.embed_states([state])
        # arrow_embedding : (B, H)
        arrow_embeddings = self.embed_arrows(arrows)
        # state_t : (H, 1)
        state_t = self.arrow_readout(state_embedding).transpose(0, 1)
        # Result: (B,)
        return arrow_embeddings.matmul(state_t).squeeze(1)

    def score_outcomes(self, outcomes: list[str], action: str, state: str) -> torch.Tensor:
        if len(outcomes) <= 1:
            return torch.ones(len(outcomes), dtype=torch.float, device=self.get_device())
        # state_embedding : (1, H)
        state_embedding = self.embed_states([state])
        # outcome_embeddings : (B, H)
        outcome_embeddings = self.embed_outcomes(outcomes)
        # state_t : (H, 1)
        state_t = self.outcome_readout(state_embedding).transpose(0, 1)
        # Result: (B,)
        return outcome_embeddings.matmul(state_t).squeeze(1)

    def estimate_values(self, states: list[str]) -> torch.Tensor:
        logger.debug('Estimating values for %d states, maxlen = %d',
                     len(states), max(map(len, states)))
        state_embedding = self.embed_states(states)
        return self.value_readout(state_embedding).squeeze(1)

    def get_device(self):
        return self.embedding.weight.device

    def extract_examples(self, episode, random_negatives=[]) -> list[ContrastivePolicyExample]:
        examples = []

        if not episode.success and self.discard_unsolved:
            return examples

        if isinstance(episode, Episode):
            for i, a in enumerate(episode.actions):
                if episode.success:
                    if episode.negative_actions[i]:
                        examples.append(DBExample(
                            state=episode.states[i],
                            actions=[a] + episode.negative_actions[i],
                            next_state=episode.states[i+1],
                            is_terminal=i == len(episode.actions) - 1
                        ))
                        # examples.append(ContrastivePolicyExample(type=ExampleType.STATE_ACTION,
                        #                                          state=episode.states[i],
                        #                                          positive=a,
                        #                                          negatives=episode.negative_actions[i]))

                        # if random_negatives and \
                        #    random.random() < self.solution_augmentation_probability:
                        #     examples.extend(self._perform_augmentation(episode, random_negatives))

            # if self.train_value_function:
            #     for i, s in enumerate(episode.states):
            #         value = (0 if not episode.success
            #                  else self.discount ** (len(episode.states) - (i + 1)))
            #         examples.append(ContrastivePolicyExample(type=ExampleType.STATE_VALUE,
            #                                                  state=episode.states[i],
            #                                                  value=value))
        elif isinstance(episode, TreeSearchEpisode):
            for node in episode.visited:
                examples.append(ContrastivePolicyExample(type=ExampleType.STATE_VALUE,
                                                         state=node.state,
                                                         value=node.value_target))

        return examples

    def _perform_augmentation(self, episode, random_negatives):
        # Add a few random negatives to the solution
        augmented_actions = []

        # How many to insert.
        n_insertions = np.random.geometric(p=self.solution_augmentation_rate)
        # Where to insert random steps.
        queue = sorted(random.choices(list(range(len(episode.actions) // 2)),
                                      k=n_insertions), reverse=True)
        is_augmentation = []
        positives, negatives = [], []

        for i in range(len(episode.actions) // 2):
            while queue and queue[-1] == i:
                queue.pop()
                augmented_actions.extend(random.choice(random_negatives))

                if random.random() < 0.5:
                    augmented_actions[-1] = '_'

                positives.extend(episode.actions[2*i:2*i+2])
                negatives.extend(episode.negative_actions[2*i:2*i+2])
                is_augmentation.append(True)

            augmented_actions.extend(episode.actions[2*i:2*i+2])
            positives.extend(episode.actions[2*i:2*i+2])
            negatives.extend(episode.negative_actions[2*i:2*i+2])
            is_augmentation.append(False)

        augmentations = []

        states = Solution.states_from_episode(episode.problem, episode.goal,
                                              augmented_actions)

        assert len(states) == 1 + len(augmented_actions)

        for i in range(len(is_augmentation)):
            if i and is_augmentation[i-1]:
                augmentations.append(ContrastivePolicyExample(
                    type=ExampleType.STATE_ACTION,
                    state=states[2*i],
                    positive=positives[2*i],
                    negatives=negatives[2*i]
                ))

        return augmentations

    def score_arrows_batch(self, batch_arrows: list[list[str]], batch_states: list[str]) -> torch.Tensor:
        # Compute state embeddings for the entire batch (batch_size, H)
        state_embeddings = self.embed_states(batch_states)
        
        # Compute arrow embeddings for the entire batch and pad them
        max_num_arrows = max(len(arrows) for arrows in batch_arrows)
        padded_arrows = [arrows + [EMPTY] * (max_num_arrows - len(arrows)) for arrows in batch_arrows]  # padding with empty strings
        flat_arrows = [arrow for arrows in padded_arrows for arrow in arrows]  # flatten the list of lists

        # Embed all arrows in the batch and reshape them back to (batch_size, max_num_arrows, H)
        arrow_embeddings = self.embed_arrows(flat_arrows).view(len(batch_states), max_num_arrows, -1)
        
        # Compute dot product scores between state and arrows
        state_transformed = self.arrow_readout(state_embeddings).unsqueeze(1)  # (batch_size, 1, H)
        scores = torch.bmm(arrow_embeddings, state_transformed.transpose(1, 2)).squeeze(-1)  # (batch_size, max_num_arrows)

        # Create a mask for padded arrows
        mask = torch.tensor([[0 if arrow != EMPTY else -1e9 for arrow in arrows] for arrows in padded_arrows], device=scores.device)
        
        # Apply mask to ignore padded elements
        scores = scores + mask.float()

        return scores

    def get_loss_batch(self, batch) -> torch.Tensor:
        log_rews = torch.tensor([0 if e.success else -20 for e in batch], device=self.get_device())
        lens = [len(e.actions) for e in batch]
        max_len = max(lens)
        log_probs = torch.zeros((len(batch), max_len), device=self.get_device())

        for step in range(max_len):
            states = []
            actions = []
            for e in batch:
                try:
                    states.append(e.states[step] if step < len(e.states) - 1 else e.states[-1])
                except:
                    import pdb; pdb.set_trace();
                actions.append(([e.actions[step]] + e.negative_actions[step]) if step < len(e.actions) else [e.actions[-1]])
            mask = torch.tensor([step < l for l in lens]).to(self.get_device())
            if step == 0:
                logz = self.estimate_values(states).squeeze(0)
            
            action_probs = self.score_arrows_batch(actions, states)
            action_log_probs = F.log_softmax(action_probs, dim=1)
            log_probs[:, step] = action_log_probs[:, 0] * mask
        loss = ((log_probs.sum(1) - log_rews + logz) ** 2).mean()
        return loss

    def get_db_loss_batch(self, batch) -> torch.Tensor:
        states = [example.state for example in batch]
        actions = [example.actions for example in batch]
        next_states = [example.next_state for example in batch]
        mask = torch.tensor([not example.is_terminal for example in batch], device=self.get_device())

        logF = self.estimate_values(states).squeeze(0)
        logF_next = self.estimate_values(next_states).squeeze(0)
        action_probs = self.score_arrows_batch(actions, states)
        action_log_probs = F.log_softmax(action_probs, dim=1)
        log_pfs = action_log_probs[:, 0]
        loss = (logF - logF_next + log_pfs) ** 2

        return loss.mean()

    def get_neg_loss_batch(self, batch) -> torch.Tensor:
        lens = [len(e.actions) for e in batch]
        max_len = max(lens)
        log_probs = torch.zeros((len(batch), max_len), device=self.get_device())

        for step in range(max_len):
            states = []
            actions = []
            for e in batch:
                try:
                    states.append(e.states[step] if step < len(e.states) - 1 else e.states[-1])
                except:
                    import pdb; pdb.set_trace();
                actions.append(([e.actions[step]] + e.negative_actions[step]) if step < len(e.actions) else [e.actions[-1]])
            mask = torch.tensor([step < l for l in lens]).to(self.get_device())
            
            action_probs = self.score_arrows_batch(actions, states)
            action_log_probs = F.log_softmax(action_probs, dim=1)
            log_probs[:, step] = action_log_probs[:, 0] * mask
        loss = (log_probs.sum(1)).mean()
        return loss

    def get_loss(self, batch) -> torch.Tensor:
        losses = []
        for ep in batch:
            if len(ep.states) == 0:
                continue
            lp = 0
            log_rew = 0 if ep.success else -20
            logz = self.estimate_values([ep.states[0]]).squeeze(0)
            for i, (st, a) in enumerate(zip(ep.states[:-1], ep.actions)):
                action_probs = self.score_arrows([a] + ep.negative_actions[i], st)
                action_log_probs = F.log_softmax(action_probs, dim=0)[0]
                lp += action_log_probs
            loss = (lp - log_rew + logz) ** 2
            losses.append(loss)
        loss = torch.stack(losses, dim=0)
        return loss.mean()

    def embed_raw(self, strs: list[str]) -> torch.Tensor:
        strs = [s[:self.max_len] for s in strs]
        outputs = []

        for b in batch_strings(strs, batch_size=4096):
            input = encode_batch(b, self.get_device(), bos=True, eos=True)
            input = self.embedding(input.transpose(0, 1))
            lm_output, _ = self.lm(input)
            outputs.append(lm_output[0, :, :])

        return torch.cat(outputs, dim=0)

    def fit_tb(self,
               dataset: list[Episode],
               checkpoint_callback=lambda: None):
        self.train()

        optimizer = torch.optim.Adam(self.parameters(), lr=self.lr)

        positives = [i for i, e in enumerate(dataset) if e.success]
        negatives = [i for i, e in enumerate(dataset) if not e.success]
        losses = []
        neg_losses = []
        for e in range(self.gradient_steps):
            optimizer.zero_grad()
            if not self.positives_only:
                batch_pos_idx = random.sample(positives, k=min(len(positives), self.batch_size // 2))
                batch_neg_idx = random.sample(negatives, k=min(len(negatives), self.batch_size // 2))
            else:
                batch_pos_idx = random.sample(positives, k=min(len(positives), self.batch_size))
                batch_neg_idx = []
            batch_pos = [dataset[i] for i in batch_pos_idx if len(dataset[i].states) > 0 and len(dataset[i].actions) > 0 and dataset[i].negative_actions is not None]
            batch_neg = [dataset[i] for i in batch_neg_idx if len(dataset[i].states) > 0 and len(dataset[i].actions) > 0 and dataset[i].negative_actions is not None]
            batch = batch_pos + batch_neg

            try:
                loss = self.get_loss_batch(batch)
            except RuntimeError as e:
                if "out of memory" in str(e):
                    print("WARNING: Out of memory, skipping batch")
                    torch.cuda.empty_cache()
                    continue

            # elapsed = time.time() - start
            # print("Batch time: ", elapsed, "Loss: ", loss.item())
            # start = time.time()
            # loss = self.get_loss(batch)
            # elapsed = time.time() - start
            # print("Non-batched time: ", elapsed, "Loss: ", loss.item())
            loss.backward()
            optimizer.step()
            # if self.positives_only:
            #     neg_loss = self.train_negatives(negatives, dataset, optimizer)
            #     neg_losses.append(neg_loss.item())
            # print("Loss: ", loss.item(), "Neg loss: ", neg_loss.item())
            print("Loss: ", loss.item())
            losses.append(loss.cpu().item())
            torch.cuda.empty_cache()
            checkpoint_callback()
        
        return {
            "loss": sum(losses) / len(losses)
        }
        # return {
        #     "loss": sum(losses) / len(losses),
        #     "neg_loss": sum(neg_losses) / len(neg_losses)
        # } if self.positives_only else {
        #     "loss": sum(losses) / len(losses)
        # }

    def fit_db(self,
               dataset: list[Episode],
               checkpoint_callback=lambda: None):
        self.train()
        optimizer = torch.optim.Adam(self.parameters(), lr=self.lr)
        all_negatives = []

        for e in dataset:
            for i in range(len(e.actions) // 2):
                all_negatives.append(e.actions[2*i:2*i+2])

        # Assemble contrastive examples
        examples = []

        # import pdb; pdb.set_trace();
        for episode in dataset:
            examples.extend(self.extract_examples(episode, all_negatives))
        
        losses = []
        for e in range(self.gradient_steps):
            optimizer.zero_grad()
            batch = random.sample(examples, k=min(len(examples), self.batch_size))
            loss = self.get_db_loss_batch(batch)
            loss.backward()
            optimizer.step()
            losses.append(loss.item())
            # wandb.log({'train_loss': loss.cpu()})

            checkpoint_callback()
        return {
            'loss': np.mean(losses),
        }

    def fit(self,
            dataset: list[Episode],
            checkpoint_callback=lambda: None):
        if self.loss == "tb":
            return self.fit_tb(dataset, checkpoint_callback)
        elif self.loss == "db":
            return self.fit_db(dataset, checkpoint_callback)
        
    
    def train_negatives(self, negatives, dataset, optimizer):
        batch_neg_idx = random.sample(negatives, k=min(len(negatives), self.batch_size))
        batch_neg = [dataset[i] for i in batch_neg_idx if len(dataset[i].states) > 0 and len(dataset[i].actions) > 0 and dataset[i].negative_actions is not None]

        try:
            # loss = self.get_loss_batch(batch)
            loss = self.get_neg_loss_batch(batch_neg)
        except RuntimeError as e:
            if "out of memory" in str(e):
                print("WARNING: Out of memory, skipping batch")
                torch.cuda.empty_cache()
        
        loss.backward()
        optimizer.step()

        return loss.cpu()


class DiversityPolicyVerifier(DiversityPolicy):
    def __init__(self, config):
        super().__init__(config)
        self._lm_verifier = nn.GRU(input_size=config.gru.embedding_size,
                            hidden_size=config.gru.hidden_size,
                            bidirectional=True,
                            num_layers=config.gru.layers)
        self._verifier_readout = nn.Sequential(
            nn.Linear(2*config.gru.hidden_size, 2*config.gru.hidden_size),
            nn.ReLU(),
            nn.Linear(2*config.gru.hidden_size, 1)
        )
        self._verifier_embedding = nn.Embedding(128, config.gru.embedding_size)
        self.verifier_type = config.verifier_type
        self.decomposition_steps = config.decomposition_steps
        self.verifier_dropout_prob = config.verifier_dropout_prob
        self.pretrain_verifier = config.pretrain_verifier
        verifier_params = list(self._lm_verifier.parameters()) + list(self._verifier_readout.parameters()) + list(self._verifier_embedding.parameters())
        self.opt_verifier = torch.optim.Adam(verifier_params, lr=self.lr)

    def verifier_embed_states(self, batch: list[str]) -> torch.Tensor:
        return self.verifier_embed_raw([f'S{s}S' for s in batch])

    def verifier_embed_raw(self, strs: list[str]) -> torch.Tensor:
        strs = [s[:self.max_len] for s in strs]
        outputs = []

        for b in batch_strings(strs, batch_size=4096):
            input = encode_batch(b, self.get_device(), bos=True, eos=True)
            input = self.embedding(input.transpose(0, 1))
            lm_output, _ = self.lm(input)
            outputs.append(lm_output[0, :, :])

        return torch.cat(outputs, dim=0)
    
    def score_verifier(self, states: list[str]) -> torch.Tensor:
        logger.debug('Estimating reward for %d states, maxlen = %d',
                     len(states), max(map(len, states)))
        state_embedding = self.verifier_embed_states(states)
        return self._verifier_readout(state_embedding).squeeze(1)

    def get_db_loss_batch(self, batch) -> torch.Tensor:
        lens = [len(e.actions) for e in batch]
        max_len = max(lens)
        log_probs = torch.zeros((len(batch), max_len), device=self.get_device())
        potentials = torch.zeros((len(batch), max_len), device=self.get_device())
        logFs = torch.zeros((len(batch), max_len), device=self.get_device())

        for step in range(max_len):
            states = []
            actions = []
            for e in batch:
                try:
                    states.append(e.states[step] if step < len(e.states) - 1 else e.states[-1])
                except:
                    import pdb; pdb.set_trace();
                actions.append(([e.actions[step]] + e.negative_actions[step]) if step < len(e.actions) else [e.actions[-1]])
            mask = torch.tensor([step < l for l in lens]).to(self.get_device())
            logF = self.estimate_values(states).squeeze(0)
            # if step == 0:
            #     logz = self.estimate_values(states).squeeze(0)
            action_probs = self.score_arrows_batch(actions, states)
            action_log_probs = F.log_softmax(action_probs, dim=1)
            log_probs[:, step] = action_log_probs[:, 0] * mask
            with torch.no_grad():
                potential = self.score_verifier(states)
                potentials[:, step] = potential * mask
                for i, l in enumerate(lens):
                    if step == l-1:
                        potentials[i, step] = potentials[i, :step].sum() - (0 if batch[i].success else -20)
            logFs[:, step] = logF * mask
        loss = logFs[:, :-1] + log_probs[:, :-1] - potentials[:, :-1] - logFs[:, 1:]
        return loss.pow(2).mean()

    def train_verifier_led_step(self, batch):
        self.opt_verifier.zero_grad()
        lens = [len(e.actions) for e in batch]
        max_len = max(lens)
        mask_running = torch.zeros((len(batch)), device=self.get_device())
        potentials = torch.zeros((len(batch), max_len), device=self.get_device())
        for step in range(max_len):
            states = []
            actions = []
            for e in batch:
                try:
                    states.append(e.states[step] if step < len(e.states) - 1 else e.states[-1])
                except:
                    import pdb; pdb.set_trace();
                actions.append(([e.actions[step]] + e.negative_actions[step]) if step < len(e.actions) else [e.actions[-1]])
            mask = torch.tensor([step < l for l in lens]).to(self.get_device())
            potential = self.score_verifier(states)
            potentials[:, step] = potential * mask
            mask_r = torch.ones(len(batch), device=self.get_device()) * self.verifier_dropout_prob
            mask_r = mask_r.bernoulli_().logical_not()

            for i, l in enumerate(lens):
                if step == l-1 and l!=1:
                    mask_r[i] = 0
                elif l == 1:
                    mask_r[i] = 1
                if step == l-2:
                    if mask_running[i] == 0:
                        mask_r[i] = 1
            potentials[:, step] = potentials[:, step] * mask_r
            mask_running += mask_r * mask

        loss = (potentials.sum(1) * (torch.tensor(lens, device=self.get_device()) / mask_running) - \
            torch.tensor([0. if e.success else -20. for e in batch], device=self.get_device())).pow(2).mean()

        loss.backward()
        self.opt_verifier.step()
        return loss.item()

    def train_verifier_linear_step(self, batch):
        self.opt_verifier.zero_grad()
        lens = [len(e.actions) for e in batch]
        max_len = max(lens)
        loss = 0
        # mask_running = torch.zeros((len(batch)), device=self.get_device())
        potentials = torch.zeros((len(batch), max_len), device=self.get_device())
        
        for step in range(max_len):
            states = []
            actions = []
            for e in batch:
                try:
                    states.append(e.states[step] if step < len(e.states) - 1 else e.states[-1])
                except:
                    import pdb; pdb.set_trace();
                actions.append(([e.actions[step]] + e.negative_actions[step]) if step < len(e.actions) else [e.actions[-1]])
            mask = torch.tensor([step < l for l in lens]).to(self.get_device())
            potential = self.score_verifier(states)
            potentials[:, step] = potential * mask
            target = torch.tensor([0. if e.success else -20. for e in batch], device=self.get_device()) / torch.tensor(lens, device=self.get_device())
            target = target * mask
            loss += F.mse_loss(potential, target)

        loss.backward()
        self.opt_verifier.step()
        return loss.item()

    def fit(self,
            dataset: list[Episode],
            checkpoint_callback=lambda: None):
        self.train()
        policy_params = list(self.lm.parameters()) + list(self.arrow_readout.parameters()) + \
            list(self.outcome_readout.parameters()) + list(self.value_readout.parameters()) + list(self.embedding.parameters())
        optimizer = torch.optim.Adam(policy_params, lr=self.lr)

        positives = [i for i, e in enumerate(dataset) if e.success]
        negatives = [i for i, e in enumerate(dataset) if not e.success]
        losses = []
        all_verifier_losses = []

        if self.pretrain_verifier:
            for e in range(self.gradient_steps):
                verifier_losses = []
                batch_pos_idx = random.sample(positives, k=min(len(positives), self.batch_size // 2))
                batch_neg_idx = random.sample(negatives, k=min(len(negatives), self.batch_size // 2))
                batch_pos = [dataset[i] for i in batch_pos_idx if len(dataset[i].states) > 0 and len(dataset[i].actions) > 0 and dataset[i].negative_actions is not None]
                batch_neg = [dataset[i] for i in batch_neg_idx if len(dataset[i].states) > 0 and len(dataset[i].actions) > 0 and dataset[i].negative_actions is not None]
                batch = batch_pos + batch_neg
                if self.verifier_type== "LED":
                    for _ in range(self.decomposition_steps):
                        verifier_losses.append(self.train_verifier_led_step(batch))
                elif self.verifier_type== "linear":
                    verifier_losses.append(self.train_verifier_linear_step(batch))
                all_verifier_losses.append(np.mean(verifier_losses))
                print("Pretrain Verifier Loss: ", np.mean(verifier_losses))



        for e in range(self.gradient_steps):
            optimizer.zero_grad()
            batch_pos_idx = random.sample(positives, k=min(len(positives), self.batch_size // 2))
            batch_neg_idx = random.sample(negatives, k=min(len(negatives), self.batch_size // 2))
            batch_pos = [dataset[i] for i in batch_pos_idx if len(dataset[i].states) > 0 and len(dataset[i].actions) > 0 and dataset[i].negative_actions is not None]
            batch_neg = [dataset[i] for i in batch_neg_idx if len(dataset[i].states) > 0 and len(dataset[i].actions) > 0 and dataset[i].negative_actions is not None]
            batch = batch_pos + batch_neg

            try:
                loss = self.get_db_loss_batch(batch)
            except RuntimeError as e:
                if "out of memory" in str(e):
                    print("WARNING: Out of memory, skipping batch")
                    torch.cuda.empty_cache()
                    continue
            
            loss.backward()
            optimizer.step()
            losses.append(loss.cpu().item())

            verifier_losses = []
            if not self.pretrain_verifier:
                if self.verifier_type== "LED":
                    for _ in range(self.decomposition_steps):
                        verifier_losses.append(self.train_verifier_led_step(batch))
                elif self.verifier_type== "linear":
                    verifier_losses.append(self.train_verifier_linear_step(batch))
            print("Loss: ", loss.item(), "| Verifier Loss: ", np.mean(verifier_losses))
            all_verifier_losses.append(np.mean(verifier_losses))
            torch.cuda.empty_cache()
            checkpoint_callback()
            
        return {
            "loss": sum(losses) / len(losses),
            "verifier_loss": np.mean(all_verifier_losses)
        }
    

class TestDataPreparation(unittest.TestCase):
    def test_solution_augmentation(self):
        import omegaconf
        cfg = omegaconf.DictConfig({
            'gru': {'hidden_size': 10, 'embedding_size': 10, 'layers': 1},
            'discard_unsolved': True,
            'train_value_function': False,
            'batch_size': 10,
            'lr': 1e-5,
            'gradient_steps': 10,
            'solution_augmentation_probability': 1.0,
            'solution_augmentation_rate': 0.2,
        })
        policy = ContrastivePolicy(cfg)
        episode = Episode('(= x (+ 10 20))', '(= x ?)', 'subst-eval', True,
                          actions=['eval', '(= (+ 10 20) 30)', 'rewrite', '(= x 30)'],
                          states=["s1", "s2", "s3", "s4", "s5"],
                          negative_actions=[['a', 'b'], ['o1'], ['a', 'b'], ['o2']])

        e = policy.extract_examples(episode, [('haha', 'hoho'), ('hihi', 'hehe')])
        assert len(e) > 4


def make_policy(config):
    if 'type' not in config:
        raise ValueError(f'Policy config must have a \'type\'')
    if config.type == 'DecisionTransformer':
        return DecisionTransformer(config)
    elif config.type == 'DecisionGRU':
        return DecisionGRU(config)
    elif config.type == 'ContrastivePolicy':
        return ContrastivePolicy(config)
    raise ValueError(f'Unknown policy type {config.type}')


if __name__ == '__main__':
    import environment
    from omegaconf import DictConfig, OmegaConf
    d = make_domain('comb-like')

    cfg = DictConfig({'reformer': {'hidden_size': 256,
                                   'n_hidden_layers': 1,
                                   'n_attention_heads': 1},
                      'gru': {'hidden_size': 64,
                              'embedding_size': 64,
                              'layers': 2},
                      'discard_unsolved': True,
                      'batch_size': 512})

    # policy = DecisionTransformer(cfg, arrows)
    # policy = DecisionGRU(cfg)
    # policy = ContrastivePolicy(cfg)
    policy = RandomPolicy(cfg)
    # policy = policy.to(torch.device(1))
    policy.eval()

    problem = d.generate_derivation(2)

    import time
    before = time.time()

    # logging.basicConfig(level=logging.DEBUG)

    episode = policy.beam_search(d, problem, depth=8, beam_size=10)
    print(time.time() - before)

    print('Episode:', episode)
    print('Last state length:', len(episode.states[-1]))
