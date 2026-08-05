#!/usr/bin/env bash
# Switch rancor's Slurm between the two dependency-handling profiles.
#
#   sudo ./profile.sh permissive   # DependencyParameters unset (the default)
#   sudo ./profile.sh strict       # kill_invalid_depend, plus enforced limits
#   ./profile.sh                   # report which profile is live
#
# Why two. Slurm's default is that a job whose dependency failed **pends
# forever** with reason DependencyNeverSatisfied, and never appears in sacct as
# failed at all. With DependencyParameters=kill_invalid_depend it is cancelled
# instead, and the same experiment leaves a completely different trail. Sites
# differ, so the workflow has to handle both and depend on neither, and the
# only way to know it does is to run the tier 2 suite under each:
#
#   sudo tools/slurm/profile.sh permissive && pytest tests/test_tier2.py
#   sudo tools/slurm/profile.sh strict     && pytest tests/test_tier2.py
#
# The strict profile also imposes a small MaxSubmitJobs, so that the projected
# job count check in `ackbar validate` step 5 is exercised against a limit that
# actually exists rather than against rancor's 10000.
#
# Docs: ../../docs/slurm.md

set -euo pipefail

CONF=/etc/slurm/slurm.conf
QOS=ackbar-limited
MAX_SUBMIT=200

live() {
  local dep
  dep=$(scontrol show config | awk -F'= *' '/^DependencyParameters/ {print $2}')
  [[ $dep == *kill_invalid_depend* ]] && echo strict || echo permissive
}

case "${1:-}" in
  "" | show)
    echo "profile: $(live)"
    scontrol show config | grep -iE '^(DependencyParameters|MaxJobCount)'
    sacctmgr -nP show qos "$QOS" format=Name,MaxSubmitJobsPU 2>/dev/null || true
    exit 0
    ;;
  permissive | strict) want=$1 ;;
  *) echo "usage: $0 [permissive|strict|show]" >&2; exit 2 ;;
esac

[[ $EUID -eq 0 ]] || { echo "run me with sudo" >&2; exit 1; }

# The setting is absent by default, so removing the line is what "permissive"
# means. Editing in place rather than through install.sh, deliberately: this is
# a knob to be flipped back and forth during a test run, not a config change to
# be recorded.
sed -i '/^DependencyParameters=/d' "$CONF"
if [[ $want == strict ]]; then
  printf 'DependencyParameters=kill_invalid_depend\n' >> "$CONF"
fi

# Limits live on a QOS rather than in slurm.conf, because MaxSubmitJobs is an
# association property and slurm.conf has no per-user equivalent.
if [[ $want == strict ]]; then
  sacctmgr -i add qos "$QOS" set MaxSubmitJobsPerUser=$MAX_SUBMIT >/dev/null 2>&1 || \
    sacctmgr -i modify qos "$QOS" set MaxSubmitJobsPerUser=$MAX_SUBMIT >/dev/null
  sacctmgr -i modify user "$SUDO_USER" set qos=normal,"$QOS" defaultqos="$QOS" >/dev/null
else
  sacctmgr -i modify user "$SUDO_USER" set defaultqos=normal >/dev/null 2>&1 || true
fi

scontrol reconfigure
sleep 1
echo "profile: $(live)"
