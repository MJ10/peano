#!/bin/bash
#SBATCH --partition=long                      # Ask for unkillable job
#SBATCH --cpus-per-task=8                    # Ask for 2 CPUs
#SBATCH --gres=gpu:1                         # Ask for 1 GPU
#SBATCH --mem=16G                             # Ask for 10 GB of RAM
#SBATCH --time=18:00:00                        # The job will run for 3 hours
#SBATCH -o /network/scratch/m/moksh.jain/logs/peano-%j.out  # Write the log on tmp1


module load python/3.9 cuda/11.8
export PYTHONUNBUFFERED=1

source $SCRATCH/peano_env/bin/activate

cd ~/peano/learning/

python trainer.py