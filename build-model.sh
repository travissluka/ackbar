#!/bin/bash
# Build MOM6-SIS2 (ice_ocean_SIS2 / coupler_main) from ~/work/ackbar/pkg/mom6sis2
# using the spack-stack toolchain. See docs/model-build.md.
set -e

MODEL_DIR=${MODEL_DIR:-$(dirname "$(readlink -f "$0")")/pkg/mom6sis2}
TARGET=${TARGET:-ice_ocean_SIS2}

source ~/work/env.sh

export CC=mpicc
export MPICC=mpicc
export FC=mpif90
export MPIFC=mpif90
export CFLAGS="-g -O2"
export FCFLAGS="-g -O2 -fallow-argument-mismatch -fallow-invalid-boz"
# env.sh puts "-fuse-ld=mold" in LDFLAGS for cmake; harmless here, keep it.

cd "$MODEL_DIR"
exec make -j"${NJOBS:-16}" "$TARGET"
