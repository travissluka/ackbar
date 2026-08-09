#!/bin/bash
# Build coupler_main against the real NOAA-PSL stochastic pattern generator
# instead of the stub MOM6 ships.
#
#   tools/spike-build-stochastic.sh
#
# MOM6 carries the *interface* to ocean stochastic physics
# (`src/parameterizations/stochastic/MOM_stochastics.F90`, which reads DO_SPPT,
# DO_SKEB and PERT_EPBL) but not the pattern generator behind it. What it ships
# is `config_src/external/stochastic_physics`, seventy lines that return a
# nonzero code for any of the three, which `MOM_stochastics.F90` turns into a
# FATAL. So the stock build can read the parameters and cannot run them.
#
# The generator lives at github.com/NOAA-PSL/stochastic_physics, the same one
# the UFS uses, and it is a CMake library while MOM6-examples is autoconf. The
# join is not a port: `configure.ice_ocean.ac` appends $EXTRA_SRC_DIRS to the
# directories makedep walks, so the real sources compile alongside MOM6's own
# as long as the stub is not also present to define the module twice. Hence the
# copy of `src/MOM6` with that one directory removed: the checkout's submodule
# is left alone.
#
# Precision lines up without being made to. MOM6's autoconf adds
# `-fdefault-real-8 -fdefault-double-8`, which is exactly what the generator's
# CMakeLists sets for its own 64-bit path, so the `real` crossing the interface
# is 64 bits on both sides. If MOM6 is ever built 32-bit this stops being true
# silently, in the worst way: the arguments still match by name and rank.
set -euo pipefail

ACKBAR_ROOT=$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)
source "$ACKBAR_ROOT/site/activate.sh"

# The checkout that owns the model. The worktree a spike is developed in has no
# populated `pkg/`, and nothing here writes into this tree.
MODEL_DIR=${MODEL_DIR:-/home/tsluka/work/ackbar/pkg/mom6sis2}
SPIKE=${SPIKE:-/data/ackbar/spike}
STOCHY=${STOCHY:-$SPIKE/stochastic_physics}
# `configure.ice_ocean.ac` looks for SIS2's memory header at
# `${srcdir}/../SIS2`, so the modified MOM6 has to keep its siblings. Hence a
# whole `src` alongside it, every entry a symlink back to the checkout except
# MOM6 itself.
SRC=$SPIKE/src-stoch
CODEBASE=$SRC/MOM6
BUILD=$SPIKE/build-stoch

[[ -d $STOCHY ]] || {
    echo "spike-build-stochastic: clone NOAA-PSL/stochastic_physics to $STOCHY first" >&2
    exit 1
}

mkdir -p "$SRC"
for path in "$MODEL_DIR"/src/*; do
    name=$(basename "$path")
    [[ $name == MOM6 ]] && continue
    ln -sfn "$path" "$SRC/$name"
done

# The MOM6 the stochastic build compiles: upstream's, minus the stub.
if [[ ! -d $CODEBASE ]]; then
    echo "copying $MODEL_DIR/src/MOM6 -> $CODEBASE"
    cp -r "$MODEL_DIR/src/MOM6" "$CODEBASE"
fi
rm -rf "$CODEBASE/config_src/external/stochastic_physics"

export CC=mpicc MPICC=mpicc FC=mpif90 MPIFC=mpif90
export CFLAGS="-g -O2"
export FCFLAGS="-g -O2 -fallow-argument-mismatch -fallow-invalid-boz"

# The generator's spectral transform calls `esmf_dgemm`, which is ESMF's copy
# of the BLAS routine, and this build links neither ESMF nor BLAS. The shim
# supplies the name over openblas; see the file for why not `-DCESMCOUPLED`.
BLAS=$(dirname "$(readlink -f /home/tsluka/work/spack-stack/envs/skylab.gcc/install/gcc/13.3.0/openblas-*/lib/libopenblas.so)")

EXTRA="$MODEL_DIR/src/SIS2/src \
$MODEL_DIR/src/SIS2/config_src/external/Icepack_interfaces \
$MODEL_DIR/src/coupler \
$STOCHY \
$ACKBAR_ROOT/tools/stochastic-shim"

export LDFLAGS="${LDFLAGS:-} -L$BLAS -Wl,-rpath,$BLAS"
export LIBS="${LIBS:-} -lopenblas"

make -C "$MODEL_DIR/ice_ocean_SIS2" \
     BUILD="$BUILD" \
     MOM_CODEBASE="$CODEBASE" \
     EXTRA_SRC_DIRS="$EXTRA" \
     -j"${NJOBS:-4}" \
     "$BUILD/coupler_main"

echo
ls -la "$BUILD/coupler_main"
