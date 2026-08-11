#!/bin/bash
# Toolchain for Orion (MSU HPC2).
#
# Sourced by site/activate.sh, which names it through ACKBAR_ENV_SETUP in
# site/orion.sh. Nothing here may set an ACKBAR_* variable: this file answers
# "what compiler and libraries", the site file answers "what paths and queues".
#
# Ported from JCSDA-internal/jedi-tools, buildscripts/setup/orion_setup_oneapi.sh,
# which is what ewok and skylab load on this machine. The module paths below are
# theirs; when a JEDI release moves to a newer spack-stack, that file moves first
# and this one follows it rather than leading.
#
# oneapi rather than gcc because it is the stack JCSDA maintains on Orion: the
# jedi-tools setup directory has an orion_setup_oneapi.sh and no gcc equivalent.
# See "Compilers" in site/README.md for what to change if the model build
# rejects it.

module purge
module use /apps/contrib/spack-stack/modulefiles
module use /apps/contrib/spack-stack/spack-stack-2.1.0/envs/ue-oneapi-2025.3.1/modules/Core
module load stack-intel-oneapi-compilers/2025.3.1
module load stack-intel-oneapi-mpi/2021.17
module load stack-python

# HDF5's POSIX file locking is not usable on this machine's parallel filesystem,
# and the failure is an open() error deep inside NetCDF rather than anything
# naming a lock. Every JCSDA slurm host template sets this (ewok hosts/slurm.h)
# and so does every workflow that has ever run here.
export HDF5_USE_FILE_LOCKING=FALSE

# srun otherwise starts a job step with a reduced environment on some Slurm
# configurations, and a task that sourced activate.sh loses it at the launcher.
export SLURM_EXPORT_ENV=ALL
