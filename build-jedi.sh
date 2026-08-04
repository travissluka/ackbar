#!/bin/bash
# Build the ACKBAR JEDI bundle (SOCA and its dependencies) from
# ~/work/ackbar/pkg/jedi using the spack-stack toolchain.
# See pkg/jedi/CMakeLists.txt for what is in the bundle and why.
set -e

SRC_DIR=${SRC_DIR:-$(dirname "$(readlink -f "$0")")/pkg/jedi}
BUILD_DIR=${BUILD_DIR:-$SRC_DIR/build}

source ~/work/env.sh

# env.sh selects Ninja, ccache and mold. A build directory keeps whatever
# generator it was first configured with, so delete it rather than reconfigure
# after changing generator, compiler, or modules.
cmake -S "$SRC_DIR" -B "$BUILD_DIR" \
      -DCMAKE_BUILD_TYPE="${BUILD_TYPE:-RelWithDebInfo}" \
      "$@"

exec cmake --build "$BUILD_DIR" -j "${NJOBS:-16}"
