"""Grid search to tune hyperparameters"""
import os
import sys
from subprocess import Popen, PIPE, call

import numpy as np
import wandb
import torch
import yaml

from pathlib import Path
from logger import get_clean_logger
from neural_net import load_and_preprocess, get_model, get_tensor_batch, checkpoint, resume, CustomLoss

LOG = get_clean_logger(logger_name = Path(__file__).name)
PROJECT = 'Astronomy 98'
with open('/n/home04/aboesky/berger/Weird_Galaxies/sweep_config.yaml', 'r') as f:
    SWEEP_CONFIG = yaml.safe_load(f)
# SWEEP_BASH_SCRIPT = """#!/bin/bash

# #SBATCH -c 48                                       # Number of cores (-c)
# #SBATCH --job-name={job_name}                       # This is the name of your job
# #SBATCH --mem=184G                                  # Memory pool for all cores (see also --mem-per-cpu)
# #SBATCH -t 0-12:00                                  # Runtime in D-HH:MM, minimum of 10 minutes

# # Paths to STDOUT or STDERR files should be absolute or relative to current working directory
# #SBATCH -o cluster_logs/myoutput_\%j.out                          # File to which STDOUT will be written, %j inserts jobid
# #SBATCH -e cluster_logs/myerrors_\%j.err                          # File to which STDERR will be written, %j inserts jobid
# #SBATCH --mail-user=aboesky@college.harvard.edu     # Send email to user

# #SBATCH -p shared

# # Remember:
# # The variable $TMPDIR points to the local hard disks in the computing nodes.
# # The variable $HOME points to your home directory.
# # The variable $SLURM_JOBID stores the ID number of your job.


# # Load modules
# #################################
# module load python/3.10.12-fasrc01
# conda activate ay98

# # Commands
# #############################
# export WANDB_API_KEY=6ecd8ea5ceb5a64219d98bc34ce67af0904f2be8
# wandb agent {sweep_id}
# # """
SWEEP_BASH_SCRIPT = """#!/bin/bash

#SBATCH -c 12                                       # Number of cores (-c)
#SBATCH --job-name={job_name}                       # This is the name of your job
#SBATCH --mem=56G                                  # Memory pool for all cores (see also --mem-per-cpu)
#SBATCH -t 0-00:10                                  # Runtime in D-HH:MM, minimum of 10 minutes

# Paths to STDOUT or STDERR files should be absolute or relative to current working directory
#SBATCH -o cluster_logs/myoutput_\%j.out                          # File to which STDOUT will be written, %j inserts jobid
#SBATCH -e cluster_logs/myerrors_\%j.err                          # File to which STDERR will be written, %j inserts jobid
#SBATCH --mail-user=aboesky@college.harvard.edu     # Send email to user

#SBATCH -p test

# Remember:
# The variable $TMPDIR points to the local hard disks in the computing nodes.
# The variable $HOME points to your home directory.
# The variable $SLURM_JOBID stores the ID number of your job.

# Export environment variables
#################################
export WANDB_API_KEY=6ecd8ea5ceb5a64219d98bc34ce67af0904f2be8

# Load modules
#################################
module load python/3.10.12-fasrc01
source activate pt2.1.0_cuda12.1

# Commands
#############################
wandb agent {sweep_id}
"""
# {
#                 'method': 'grid',
#                 'metric': {'goal': 'minimize', 'name': 'loss'},
#                 'parameters': {
#                     'batch_size': {
#                         'values': [32, 64, 128, 256, 512, 1024, 2048, 4096]
#                     },
#                     'n_epochs': {'value': 1000},
#                     'nodes_per_layer': {'values': [[18, 13,  8,  4], [18, 14, 11,  7,  4], [18, 15, 12,  9,  6,  4]]},
#                     'num_linear_output_layers': {'values': [2,3]},
#                     'learning_rate': {'values': np.linspace(0.01, 1.0, num=5).tolist()}
#                 }
# }


def train(config=None):
    """Training function to call in our weights and biases grid search."""
    # Load in data
    all_cat, all_photo, photo_train, photo_test, cat_train, cat_test, photo_err_train, photo_err_test, cat_err_train, \
            cat_err_test, photo_norm, photo_mean, photo_std, photo_err_norm, cat_norm, cat_mean, cat_std, cat_err_norm = load_and_preprocess()
    
    with wandb.init(project=PROJECT, config=config, mode='offline'):
        config = wandb.config

        ######################## MAKE NN, LOSS FN, AND OPTIMIZER  ########################
        torch.set_default_dtype(torch.float64)
        model = get_model(num_inputs=18, num_outputs=3, nodes_per_layer=config.nodes_per_layer, num_linear_output_layers=config.num_linear_output_layers)
        loss_fn = CustomLoss()
        optimizer = torch.optim.AdamW(model.parameters(), lr=config.learning_rate)


        ######################## TRAIN ########################
        # Training parameters
        n_epochs = config.n_epochs
        batch_size = int(config.batch_size)
        batches_per_epoch = int(len(cat_train) / batch_size)
        LOG.info('Batch Size = %i', batch_size)

        # Early stop stuff
        best_loss = 1E100
        best_epoch = -1

        # Grid search stuff
        wandb.watch(model, loss_fn, log="all")

        # Training loop
        for epoch in range(n_epochs):
            epoch_loss = 0
            for i in range(batches_per_epoch):

                # Get batch
                start = i * batch_size
                end = start + batch_size
                photo_batch = get_tensor_batch(photo_train, start, end)
                cat_batch = get_tensor_batch(cat_train, start, end)
                cat_err_batch = get_tensor_batch(cat_err_train, start, end)

                # Predict and gradient descent
                model.train()
                cat_pred = model(photo_batch)
                loss = loss_fn(cat_pred, cat_batch, cat_err_batch)
                epoch_loss += loss.item()
                optimizer.zero_grad()
                loss.backward()
                optimizer.step()

            avg_train_loss = (epoch_loss / batches_per_epoch)
            model.eval()
            test_pred = model(torch.from_numpy(photo_test))
            test_loss = loss_fn(test_pred, torch.from_numpy(cat_test), torch.from_numpy(cat_err_test))
            LOG.info('Epoch %i/%i finished with avg training loss = %.3f', epoch + 1, n_epochs, avg_train_loss)

            # Always store best model
            if test_loss < best_loss:
                best_loss = test_loss
                best_epoch = epoch
                wandb.log({'epoch': epoch + 1, 'loss': test_loss})
                checkpoint(model, "ay98/best_model.pkl")

            # Early stopping
            elif epoch - best_epoch >= 50:
                LOG.info('Loss has not decreased in 50 epochs, early stopping. Best test loss is %.3f', best_loss)
                break

        # Load best model
        resume(model, 'ay98/best_model.pkl')
        LOG.info('!!!Finished Training!!!')


def tune_parameters():
    """Use grid search to tune hyperparameters."""
    # Set api key
    os.environ["WANDB_API_KEY"] = '6ecd8ea5ceb5a64219d98bc34ce67af0904f2be8'

    # Make sweep and set sweep ID environment variable
    wandb.init(project=PROJECT)
    sweep_id = wandb.sweep(SWEEP_CONFIG, project=PROJECT)

    # Submit a number of agents to complete the sweep
    for i in range(2):
        job_name = f'{sweep_id}_agent_{i}'
        sbatchFile = open('submit_agent.sh', 'w')
        LOG.info('Submitting agent %i', i)
        sbatchFile.write(SWEEP_BASH_SCRIPT.format(job_name=job_name, sweep_id=sweep_id))
        sbatchFile.close()

        # Open a pipe to the sbatch command.
        sbatch_command = f'sbatch submit_agent.sh'
        proc = Popen(sbatch_command, shell=True, stdin=PIPE, stdout=PIPE, stderr=PIPE, close_fds=True)

        # Send job_string to sbatch
        if (sys.version_info > (3, 0)):
            proc.stdin.write(sbatch_command.encode('utf-8'))
        else:
            proc.stdin.write(sbatch_command)

        LOG.info('\tsbatch command: %s', sbatch_command)
        out, err = proc.communicate()
        LOG.info("\tout = %s", out)
        job_id = out.split()
        LOG.info("\tjob_id: %s", job_id)
        LOG.info("\terror: %s", err)
    # wandb.agent(sweep_id, function=train)


if __name__ == '__main__':
    tune_parameters()
