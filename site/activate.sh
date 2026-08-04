#!/bin/bash
# Select and load a site file, then activate the toolchain.
#
#   source site/activate.sh
#
# Picks site/$ACKBAR_SITE.sh, defaulting to the short hostname. Every value it
# defines is exported, so job scripts and the workflow see the same settings the
# build used. See "The site layer" in docs/design.md.

ACKBAR_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
ACKBAR_SITE=${ACKBAR_SITE:-$(hostname -s)}
_site_file=$ACKBAR_ROOT/site/$ACKBAR_SITE.sh

if [[ ! -f $_site_file ]]; then
    echo "ackbar: no site file for '$ACKBAR_SITE' ($_site_file)" >&2
    echo "ackbar: set ACKBAR_SITE, or add a site file modelled on site/rancor.sh" >&2
    return 1 2>/dev/null || exit 1
fi

# Defaults. A site file overrides only what differs.
ACKBAR_NJOBS=4
ACKBAR_BUILD_TYPE=RelWithDebInfo
ACKBAR_CMAKE_GENERATOR=            # empty means make, which is always present
ACKBAR_LAUNCHER=srun
ACKBAR_CAN_SUBMIT_FROM_COMPUTE=yes

source "$_site_file"

# The toolchain. Machine dependent, so the site file names it.
if [[ -n ${ACKBAR_ENV_SETUP:-} ]]; then
    # shellcheck disable=SC1090
    source "$ACKBAR_ENV_SETUP"
fi

# Only the generator the site asked for, never one inherited from the
# surrounding environment.
export CMAKE_GENERATOR=$ACKBAR_CMAKE_GENERATOR

export ACKBAR_ROOT ACKBAR_SITE
export ACKBAR_NJOBS ACKBAR_BUILD_TYPE ACKBAR_CMAKE_GENERATOR
export ACKBAR_DATASETS_ROOT ACKBAR_SCRATCH_ROOT ACKBAR_OUTPUT_ROOT ACKBAR_TEST_ROOT
export ACKBAR_PARTITION ACKBAR_ACCOUNT ACKBAR_LAUNCHER
export ACKBAR_MAX_SUBMIT_JOBS ACKBAR_MAX_ARRAY_SIZE ACKBAR_CAN_SUBMIT_FROM_COMPUTE

unset _site_file
