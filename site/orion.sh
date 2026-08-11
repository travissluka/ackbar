#!/bin/bash
# Site configuration for Orion (MSU HPC2).
#
# UNVERIFIED. Written from what ewok, skylab and jedi-tools do on this machine,
# by someone who has no account on it. Nothing below has been run. Every value
# marked "check" is one `tools/site-probe.sh` prints, and running that first is
# the whole of bringing this file up. See "Porting to an HPC" in site/README.md.
#
# Sourced, not executed. Selected by $ACKBAR_SITE, which defaults to the short
# hostname: the login nodes are Orion-login-1..N, so this file is reached by
# setting ACKBAR_SITE=orion rather than by landing on a particular one.
#
#   export ACKBAR_SITE=orion
#   source site/activate.sh

# --- environment -------------------------------------------------------------
ACKBAR_ENV_SETUP=$ACKBAR_ROOT/site/env/orion.sh

# --- build -------------------------------------------------------------------
# Deliberately modest: a login node is shared, and MSU asks that real builds go
# through the queue. `sbatch`ing a build is a wrapper around ./build-jedi.sh with
# the header from jedi-tools buildscripts/compiling/run_intel_build_Orion.sh.
ACKBAR_NJOBS=8
ACKBAR_BUILD_TYPE=RelWithDebInfo
ACKBAR_CMAKE_GENERATOR=              # make; ninja is not assumed present here

# The oneapi wrappers, because `mpicc` and `mpif90` under Intel MPI wrap gcc and
# gfortran rather than icx and ifx, and a model built that way links intel-built
# libraries with a GNU-compiled MOM6. The default flags go with them: ifx has no
# -fallow-argument-mismatch (it is a gfortran workaround for MPI's non-uniform
# interfaces, which ifx does not need) and rejects the option outright.
ACKBAR_MPICC=mpiicx
ACKBAR_MPIFC=mpiifx
ACKBAR_CFLAGS="-g -O2"
ACKBAR_FCFLAGS="-g -O2 -fp-model source"

# --- offline tools -----------------------------------------------------------
ACKBAR_MPI_TASKS=8                   # matches the domain layer's rank counts
# MPI on a login node is forbidden here, so the offline tools take their own
# allocation instead of running where they were typed. This makes each of them
# block until the scheduler starts it. Run several by hand inside one `salloc`
# if that is tiresome: srun inside an existing allocation uses it rather than
# asking for another.
ACKBAR_OFFLINE_LAUNCHER="srun --mpi=pmi2 -n"

# --- data roots --------------------------------------------------------------
# The one line to check. Everything below is derived from it, so a different
# project or a different filesystem is a single edit. /work2 rather than /work
# because that is where the JCSDA role account keeps skylab's static data, but
# an allocation may sit on either.
_work=${ACKBAR_WORK:-/work2/noaa/jcsda/$USER}

ACKBAR_DATASETS_ROOT=$_work/mom6-datasets
ACKBAR_STATIC_ROOT=$_work/ackbar/static
ACKBAR_SCRATCH_ROOT=$_work/ackbar/scratch
ACKBAR_OUTPUT_ROOT=$_work/ackbar/exp
ACKBAR_TEST_ROOT=$_work/ackbar/test

# --- scheduler ---------------------------------------------------------------
# From jedi-tools buildscripts/compiling/run_intel_build_Orion.sh, which is the
# only place in the JCSDA workflow repositories that spells an Orion queue.
# check: sacctmgr show assoc user=$USER format=account,partition,qos
ACKBAR_PARTITION=orion
ACKBAR_ACCOUNT=da-cpu
ACKBAR_QOS=batch

# Spelled out rather than relying on MpiDefault, which differs per site and
# fails silently when wrong: N ranks each in their own MPI_COMM_WORLD, N outputs
# written over each other, and Slurm records COMPLETED. Verify it with a program
# that prints MPI_Comm_size, never with an exit code. See docs/slurm.md.
# check: srun --mpi=list
ACKBAR_LAUNCHER="srun --mpi=pmi2"

# Slurm's own defaults, which are almost certainly not what limits you here: the
# cap that bites on a shared machine is the QoS per-user one.
# check: scontrol show config | grep -E 'MaxJobCount|MaxArraySize'
# check: sacctmgr show qos format=name,maxsubmitpu,maxjobspu
ACKBAR_MAX_SUBMIT_JOBS=10000
ACKBAR_MAX_ARRAY_SIZE=1000

# check: submit anything trivial from inside a job and see whether it lands. If
# it does not, cycle 1 runs and cycle 2 is never submitted, silently, because
# the job that submits the next cycle is itself a compute job. Nothing reads
# this yet; see "Known gaps" in site/README.md.
ACKBAR_CAN_SUBMIT_FROM_COMPUTE=yes

unset _work
