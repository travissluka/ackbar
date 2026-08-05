#!/bin/bash
# Site configuration for rancor.
#
# This is the ONE file that may name a path specific to this machine. Nothing
# else in ackbar may. See "The site layer" in docs/design.md.
#
# Sourced, not executed. Selected by $ACKBAR_SITE, defaulting to the short
# hostname, via site/activate.sh.

# --- environment -------------------------------------------------------------
# spack-stack lives in a different place on every machine. On rancor it is
# reached through a personal environment file that other projects also use.
ACKBAR_ENV_SETUP=~/work/env.sh

# --- build -------------------------------------------------------------------
ACKBAR_NJOBS=16                       # 8 physical cores, 16 threads
ACKBAR_BUILD_TYPE=RelWithDebInfo
# Make is the default everywhere. rancor has ninja, ccache and mold, and
# ~/work/env.sh exports CMAKE_GENERATOR=Ninja, so opt in explicitly here rather
# than inheriting it by accident.
ACKBAR_CMAKE_GENERATOR=Ninja

# --- data roots --------------------------------------------------------------
ACKBAR_DATASETS_ROOT=/data/mom6-datasets
ACKBAR_SCRATCH_ROOT=/data/ackbar/scratch
ACKBAR_OUTPUT_ROOT=/data/ackbar/exp
ACKBAR_TEST_ROOT=/data/ackbar/test

# --- scheduler ---------------------------------------------------------------
ACKBAR_PARTITION=compute
ACKBAR_ACCOUNT=ackbar
ACKBAR_LAUNCHER="srun --mpi=pmi2"     # spelled out rather than relying on
                                      # MpiDefault, which differs per site and
                                      # fails silently. See docs/slurm.md.
ACKBAR_MAX_SUBMIT_JOBS=10000          # query: scontrol show config | grep MaxJobCount
ACKBAR_MAX_ARRAY_SIZE=1000            # query: scontrol show config | grep MaxArraySize
ACKBAR_CAN_SUBMIT_FROM_COMPUTE=yes    # single node, submit host is the compute host
