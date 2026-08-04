#!/bin/bash
# Build MOM6-SIS2 (ice_ocean_SIS2 / coupler_main). See docs/model-build.md.
set -e

ACKBAR_ROOT=$(dirname "$(readlink -f "$0")")
source "$ACKBAR_ROOT/site/activate.sh"

MODEL_DIR=${MODEL_DIR:-$ACKBAR_ROOT/pkg/mom6sis2}
TARGET=${TARGET:-ice_ocean_SIS2}

export CC=mpicc
export MPICC=mpicc
export FC=mpif90
export MPIFC=mpif90
export CFLAGS="-g -O2"
export FCFLAGS="-g -O2 -fallow-argument-mismatch -fallow-invalid-boz"
# LDFLAGS may carry "-fuse-ld=mold" from the site environment; harmless here.

# MOM6-examples reaches its input data through this symlink. The root it points
# at is machine dependent, so the site file owns it.
ln -sfn "$ACKBAR_DATASETS_ROOT" "$MODEL_DIR/.datasets"

cd "$MODEL_DIR"
exec make -j"$ACKBAR_NJOBS" "$TARGET"
