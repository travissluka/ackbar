#!/usr/bin/env bash
# Populate the accounting database: cluster, account, user. Needs sudo for the
# sacctmgr writes (slurm treats root as an admin before any user is added).
#
#   sudo ./accounting-setup.sh
#
# Without this, jobs submit fine but sacct/sacctmgr associations are empty and
# any future QOS or fairshare testing has nothing to hang off.

set -euo pipefail

CLUSTER=rancor
ACCOUNT=ackbar
USER_NAME=${SUDO_USER:-$USER}

[[ $EUID -eq 0 ]] || { echo "run me with sudo" >&2; exit 1; }

sacctmgr -i add cluster "$CLUSTER"                                     || true
sacctmgr -i add account "$ACCOUNT" Description="ACKBAR ocean DA workflow" \
         Organization=jcsda Cluster="$CLUSTER"                         || true
sacctmgr -i add user "$USER_NAME" Account="$ACCOUNT" \
         DefaultAccount="$ACCOUNT" AdminLevel=Admin                    || true

echo
sacctmgr -n show cluster format=cluster,controlhost,controlport,nodecount
sacctmgr -n show assoc format=cluster,account,user,partition
