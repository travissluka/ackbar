#!/bin/bash
# Toolchain for Hercules (MSU HPC2).
#
# Sourced by site/activate.sh, which names it through ACKBAR_ENV_SETUP in
# site/hercules.sh. Nothing here may set an ACKBAR_* variable: this file answers
# "what compiler and libraries", the site file answers "what paths and queues".
#
# Ported from JCSDA-internal/jedi-tools, buildscripts/setup/hercules_setup_gcc.sh.
# jedi-tools also carries intel and oneapi setups for this machine; gcc is chosen
# here because it is the toolchain the model build's default flags are written
# for, which makes Hercules the cheaper of the two MSU machines to bring up. See
# "Compilers" in site/README.md.

module purge
module use /work/noaa/epic/role-epic/spack-stack/hercules/modulefiles
module use /apps/contrib/spack-stack/spack-stack-2.1.0/envs/ue-gcc-12.2.0/modules/Core
module load stack-gcc/12.2.0
module load stack-openmpi/4.1.4
module load stack-python

# OpenMPI here is not built against Slurm's PMIx by default, so a job step needs
# to be told which PMI to use. jedi-tools sets this in the same setup script,
# with the comment "Adding to get MPI working": without it srun starts N copies
# of a serial program, each its own MPI_COMM_WORLD of size one, and the job
# still reports COMPLETED. ACKBAR_LAUNCHER in site/hercules.sh spells the same
# thing on the command line; both are set because either alone has been enough
# to leave the other silently unused.
export SLURM_MPI_TYPE=pmi2

# See the note in site/env/orion.sh: file locking, and the environment a job
# step inherits.
export HDF5_USE_FILE_LOCKING=FALSE
export SLURM_EXPORT_ENV=ALL
