#!/bin/bash
# Select and load a site file, then activate the toolchain.
#
#   source site/activate.sh
#
# Picks site/$ACKBAR_SITE.sh, defaulting to the short hostname, or the file
# itself if ACKBAR_SITE contains a slash. Every value it defines is exported, so
# job scripts and the workflow see the same settings the build used. See "The
# site layer" in docs/design.md.

ACKBAR_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
ACKBAR_SITE=${ACKBAR_SITE:-$(hostname -s)}

# A name selects a committed site file; a path selects one anywhere. The second
# form exists so that a tool can be exercised against scratch data roots without
# a throwaway site file being committed to `site/`. Exporting a root directly
# does not work and must not start working: every tool sources this file, which
# assigns the roots unconditionally, and making those assignments defer to the
# environment would let one stale export silently redirect a real experiment's
# output. Overriding the whole site file is the deliberate act; overriding one
# variable is the accident.
case $ACKBAR_SITE in
    */*) _site_file=$ACKBAR_SITE ;;
    *)   _site_file=$ACKBAR_ROOT/site/$ACKBAR_SITE.sh ;;
esac

if [[ ! -f $_site_file ]]; then
    echo "ackbar: no site file for '$ACKBAR_SITE' ($_site_file)" >&2
    echo "ackbar: set ACKBAR_SITE, or add a site file modelled on site/rancor.sh" >&2
    return 1 2>/dev/null || exit 1
fi

# Defaults. A site file overrides only what differs.
ACKBAR_NJOBS=4
ACKBAR_MPI_TASKS=4                 # ranks for the tools that run outside Slurm
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

# The checkout this was sourced from wins the `ackbar` import, which is what
# CLAUDE.md already promises: "a running experiment imports the working tree,
# not a snapshot of it". `pip install -e` resolves to one fixed path, so without
# this a job launched from a git worktree sources that worktree's site file,
# exports that worktree's ACKBAR_ROOT, and then imports the *other* checkout's
# Python. A new key staged by code only the worktree has is silently not staged,
# and the experiment runs as though the feature were not there: a null result
# produced by a path collision rather than by the ocean.
#
# A no-op when sourced from the checkout pip was pointed at, which is the
# ordinary case.
#
# Prepended rather than assigned, because replacing it removes the interpreter's
# own additions and pytest stops finding its plugins. Guarded against being
# there twice, because this file is sourced once per submitted job and the
# duplicates accumulate.
case ":$PYTHONPATH:" in
  *":$ACKBAR_ROOT/src:"*) ;;
  *) export PYTHONPATH="$ACKBAR_ROOT/src${PYTHONPATH:+:$PYTHONPATH}" ;;
esac

# And verify it actually won. PYTHONPATH beats the `.pth` file setuptools writes
# for an editable install, but only because that strategy puts the path in
# `sys.path`. Under `editable_mode=strict` setuptools installs a meta_path
# finder instead, which is consulted *before* `sys.path` and would beat this
# silently: the worktree would export its own ACKBAR_ROOT and still import the
# other checkout, which is the exact failure the block above exists to prevent
# and the one that is invisible from inside a job.
#
# Probed through the interpreter a *job* will use, which is the venv's rather
# than `python3` from PATH: `emit.py` bakes `sys.executable` into every job
# script at create time, and the strict editable finder this guards against
# would be installed in that venv rather than in whatever PATH resolves to.
#
# A worktree has no `.venv` of its own, and that is the case this whole block
# exists for, so fall back to the checkout the worktree hangs off. Probing
# `$ACKBAR_ROOT/.venv` alone was a guard that ran zero checks in exactly the
# scenario it was written for, which is worse than the wrong interpreter it
# replaced: that one at least said something.
ackbar_python="$ACKBAR_ROOT/.venv/bin/python"
if [ ! -x "$ackbar_python" ]; then
  ackbar_main=$(git -C "$ACKBAR_ROOT" rev-parse --path-format=absolute \
                    --git-common-dir 2>/dev/null)
  [ -n "$ackbar_main" ] && ackbar_python="${ackbar_main%/.git}/.venv/bin/python"
  unset ackbar_main
fi
if [ ! -x "$ackbar_python" ]; then
  ackbar_python=$(command -v python3 2>/dev/null)
fi
if [ -n "$ackbar_python" ] && [ -x "$ackbar_python" ]; then
  ackbar_where=$("$ackbar_python" -c 'import ackbar,os;print(os.path.realpath(ackbar.__file__))' 2>/dev/null)
  case "$ackbar_where" in
    "$(cd "$ACKBAR_ROOT" && pwd -P)"/*) ;;
    # Empty means the import failed outright, which is not "fine": a venv that
    # cannot import ackbar at all runs nothing, and staying silent about it was
    # how this branch reported success for a broken install.
    "") echo "activate.sh: $ackbar_python cannot import ackbar at all." >&2
        echo "  Install it with: pip install -e $ACKBAR_ROOT" >&2 ;;
    *) echo "activate.sh: ACKBAR_ROOT is $ACKBAR_ROOT but 'import ackbar' resolves" >&2
       echo "  to $ackbar_where. Jobs would run another checkout's code." >&2
       echo "  Reinstall with: pip install -e $ACKBAR_ROOT" >&2 ;;
  esac
  unset ackbar_where
fi
unset ackbar_python

export ACKBAR_ROOT ACKBAR_SITE
export ACKBAR_NJOBS ACKBAR_MPI_TASKS ACKBAR_BUILD_TYPE ACKBAR_CMAKE_GENERATOR
export ACKBAR_DATASETS_ROOT ACKBAR_STATIC_ROOT
export ACKBAR_SCRATCH_ROOT ACKBAR_OUTPUT_ROOT ACKBAR_TEST_ROOT
export ACKBAR_PARTITION ACKBAR_ACCOUNT ACKBAR_LAUNCHER
export ACKBAR_MAX_SUBMIT_JOBS ACKBAR_MAX_ARRAY_SIZE ACKBAR_CAN_SUBMIT_FROM_COMPUTE

unset _site_file
