#!/bin/bash
# Build MOM6-SIS2 (ice_ocean_SIS2 / coupler_main). See docs/model-build.md.
set -e

ACKBAR_ROOT=$(dirname "$(readlink -f "$0")")
source "$ACKBAR_ROOT/site/activate.sh"

MODEL_DIR=${MODEL_DIR:-$ACKBAR_ROOT/pkg/mom6sis2}
STOCHY=${STOCHY:-$ACKBAR_ROOT/pkg/stochastic_physics}
TARGET=${TARGET:-ice_ocean_SIS2}

export CC=mpicc
export MPICC=mpicc
export FC=mpif90
export MPIFC=mpif90
export CFLAGS="-g -O2"
export FCFLAGS="-g -O2 -fallow-argument-mismatch -fallow-invalid-boz"
# LDFLAGS may carry "-fuse-ld=mold" from the site environment; harmless here.

# MOM6-examples reaches its input data through this symlink. The root it points
# at is machine dependent, so the site file owns it. Named here rather than left
# to `ln`, which fails on an empty target saying only "No such file or
# directory" about neither the variable nor the site file, and this is the first
# build command anyone runs on a new host.
: "${ACKBAR_DATASETS_ROOT:?is not set. build-model.sh symlinks it into the model tree as .datasets; set it in site/\$ACKBAR_SITE.sh. See docs/model-data.md}"
ln -sfn "$ACKBAR_DATASETS_ROOT" "$MODEL_DIR/.datasets"

# --- the stochastic pattern generator ----------------------------------------
#
# MOM6 carries the *interface* to ocean stochastic physics
# (`src/parameterizations/stochastic/MOM_stochastics.F90`, which reads DO_SPPT
# and PERT_EPBL) and not the generator behind it. What it ships is
# `config_src/external/stochastic_physics`, seventy lines that return a nonzero
# code for any scheme, which the interface turns into a FATAL. So a stock build
# can read the parameters and cannot run them.
#
# It is built in unconditionally rather than behind a flag, because a build with
# no scheme switched on is bit for bit a build without any of this: the
# executable that runs `experiments/osse25-letkf` reproduces the one that ran it
# before this existed. Two executables would be two things to keep in step for
# no difference in the answer.
#
# The join is not a port. `configure.ice_ocean.ac` appends $EXTRA_SRC_DIRS to
# the directories makedep walks, so the generator's sources compile alongside
# MOM6's own. Two things have to be arranged around that:
#
#  - The stub defines the same module, so makedep is told to skip that one
#    directory. `-s` rather than deleting it, which would leave the submodule
#    permanently dirty.
#  - EXTRA_SRC_DIRS is already set, by `ice_ocean_SIS2/Makefile`, to SIS2 and
#    the coupler. A command line assignment *replaces* a Makefile's own, so
#    upstream's value is read back out and appended to rather than restated
#    here: restating it is a copy that goes stale the next time the model
#    submodule moves, and the symptom is a link error a long way from the cause.
#
# Precision lines up without being made to. MOM6's autoconf adds
# `-fdefault-real-8 -fdefault-double-8`, which is exactly what the generator's
# own CMake sets for its 64-bit path, so the `real` crossing the interface is 64
# bits on both sides. A 32-bit MOM6 build would break that silently, in the
# worst way: the arguments still match by name and by rank.
[[ -d $STOCHY ]] || {
    echo "build-model: $STOCHY is missing. It is a submodule:" >&2
    echo "    git submodule update --init pkg/stochastic_physics" >&2
    exit 1
}

CASE_DIRS=$(make -s -C "$MODEL_DIR/$TARGET" \
    --eval='print-EXTRA_SRC_DIRS: ; @echo $(EXTRA_SRC_DIRS)' print-EXTRA_SRC_DIRS)

# The generator's spectral transforms call `esmf_dgemm`, ESMF's name for the
# BLAS routine, and this build links neither ESMF nor BLAS. The shim supplies
# the name over openblas; `tools/stochastic-shim/esmf_dgemm.F90` says why not
# `-DCESMCOUPLED`, which is the alternative and is global to a build that also
# compiles MOM6 and SIS2.
#
# Located by pkg-config, which the environment's own module setup points at the
# right install: openblas has no module of its own to load, but its `.pc` file
# is on `PKG_CONFIG_PATH` like every other library's. A bare `-lopenblas` does
# happen to link here, off `LD_LIBRARY_PATH`, and that is luck rather than a
# search path the linker is meant to use.
pkg-config --exists openblas || {
    echo "build-model: pkg-config cannot find openblas, which the stochastic" >&2
    echo "  pattern generator needs. Is the environment loaded?" >&2
    exit 1
}
export LDFLAGS="${LDFLAGS:-} $(pkg-config --libs-only-L openblas)"
export LIBS="${LIBS:-} $(pkg-config --libs-only-l openblas)"

cd "$MODEL_DIR"
exec make -j"$ACKBAR_NJOBS" \
    EXTRA_SRC_DIRS="$CASE_DIRS $STOCHY $ACKBAR_ROOT/tools/stochastic-shim" \
    MAKEDEP_FLAGS="-e -s $MODEL_DIR/src/MOM6/config_src/external/stochastic_physics" \
    "$TARGET"
