#!/bin/bash
# Build the ACKBAR JEDI bundle (SOCA and its dependencies).
# See pkg/jedi/CMakeLists.txt for what is in the bundle and why.
set -e

ACKBAR_ROOT=$(dirname "$(readlink -f "$0")")
source "$ACKBAR_ROOT/site/activate.sh"

SRC_DIR=${SRC_DIR:-$ACKBAR_ROOT/pkg/jedi}
BUILD_DIR=${BUILD_DIR:-$SRC_DIR/build}

# A build directory keeps whatever generator it was first configured with, so
# delete it rather than reconfigure after changing generator, compiler, or
# modules.
cmake -S "$SRC_DIR" -B "$BUILD_DIR" \
      -DCMAKE_BUILD_TYPE="$ACKBAR_BUILD_TYPE" \
      "$@"

exec cmake --build "$BUILD_DIR" -j "$ACKBAR_NJOBS"
