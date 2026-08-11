#!/bin/bash
# Site configuration for Hercules (MSU HPC2).
#
# UNVERIFIED, exactly as site/orion.sh is: written from what ewok, skylab and
# jedi-tools do here, run by nobody. Read that file alongside this one, and
# `tools/site-probe.sh` before either. See "Porting to an HPC" in site/README.md.
#
#   export ACKBAR_SITE=hercules
#   source site/activate.sh

# --- environment -------------------------------------------------------------
ACKBAR_ENV_SETUP=$ACKBAR_ROOT/site/env/hercules.sh

# --- build -------------------------------------------------------------------
ACKBAR_NJOBS=8                       # a login node is shared; prefer a build job
ACKBAR_BUILD_TYPE=RelWithDebInfo
ACKBAR_CMAKE_GENERATOR=              # make; ninja is not assumed present here

# No compiler overrides: site/env/hercules.sh loads gcc, whose wrappers and
# flags are what build-model.sh already defaults to. This is the difference
# between the two MSU machines, and the reason Hercules is the one to try first.

# --- offline tools -----------------------------------------------------------
ACKBAR_MPI_TASKS=8
ACKBAR_OFFLINE_LAUNCHER="srun --mpi=pmi2 -n"   # no MPI on the login nodes

# --- data roots --------------------------------------------------------------
# The one line to check; see site/orion.sh for what is derived from it.
_work=${ACKBAR_WORK:-/work2/noaa/jcsda/$USER}

ACKBAR_DATASETS_ROOT=$_work/mom6-datasets
ACKBAR_STATIC_ROOT=$_work/ackbar/static
ACKBAR_SCRATCH_ROOT=$_work/ackbar/scratch
ACKBAR_OUTPUT_ROOT=$_work/ackbar/exp
ACKBAR_TEST_ROOT=$_work/ackbar/test

# --- scheduler ---------------------------------------------------------------
# check: sacctmgr show assoc user=$USER format=account,partition,qos
# No JCSDA repository spells a Hercules queue, so unlike Orion's these three are
# the shape of an answer rather than an answer.
ACKBAR_PARTITION=hercules
ACKBAR_ACCOUNT=da-cpu
ACKBAR_QOS=batch

# check: srun --mpi=list. See the warning in site/orion.sh: a wrong PMI flag
# does not fail, it succeeds at running the wrong thing.
ACKBAR_LAUNCHER="srun --mpi=pmi2"

# check: scontrol show config | grep -E 'MaxJobCount|MaxArraySize'
# check: sacctmgr show qos format=name,maxsubmitpu,maxjobspu
ACKBAR_MAX_SUBMIT_JOBS=10000
ACKBAR_MAX_ARRAY_SIZE=1000

ACKBAR_CAN_SUBMIT_FROM_COMPUTE=yes   # check; nothing reads it yet

unset _work
