#!/bin/bash
#SBATCH --partition=long                      # Ask for unkillable job
#SBATCH --cpus-per-task=12                    # Ask for 2 CPUs
#SBATCH --gres=gpu:rtx8000:1                         # Ask for 1 GPU
#SBATCH --mem=32G                             # Ask for 10 GB of RAM
#SBATCH --time=10:00:00                        # The job will run for 3 hours
#SBATCH -o /network/scratch/m/moksh.jain/logs/peano-%j.out  # Write the log on tmp1


module load python/3.9 cuda/11.8
export PYTHONUNBUFFERED=1

source $SCRATCH/peano_env/bin/activate

cd ~/peano/learning/

# python trainer.py trainer.do_eval=true trainer.on_policy_frac=1. job.name=cpi_1_all
python trainer.py "$@" # trainer.do_eval=true trainer.model.type="diversity-policy" trainer.model.batch_size=32 trainer.model.gradient_steps=64 trainer.on_policy_frac=0. job.name=tb_0._all