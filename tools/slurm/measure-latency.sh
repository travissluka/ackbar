#!/usr/bin/env bash
# Measure how long Slurm takes to release a job after its dependency clears.
#
#   tools/slurm/measure-latency.sh            # default: 8 stages of 4 elements
#   tools/slurm/measure-latency.sh 12 8       # stages, array elements
#
# Needs no sudo and touches nothing outside its own jobs.
#
# Why this and not a stopwatch on the test suite. The tier 2 suite's wall clock
# is dominated by one thing: the gap between a job ending and its dependent
# starting. That gap is normally about three seconds and occasionally two
# minutes, so a single timing of the suite says almost nothing about whether a
# configuration change helped. This submits a chain deep enough for the
# distribution to show, and reports the tail rather than the mean, because the
# tail is the whole problem.
#
# Shape matches what the workflow actually submits: arrays joined elementwise
# with aftercorr, the entire chain submitted up front, one second of work each.
# A chain of scalar jobs does not reproduce it.
#
# Docs: ../../docs/slurm.md

set -euo pipefail

STAGES=${1:-8}
ELEMENTS=${2:-4}
TAG=lat$$
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

command -v sbatch >/dev/null || { echo "no sbatch on this machine" >&2; exit 1; }

cat > "$WORK/stage.sh" <<'EOF'
#!/bin/bash
#SBATCH --ntasks=1
#SBATCH --mem=200M
#SBATCH --time=00:02:00
sleep 1
EOF

echo "submitting $STAGES stages of $ELEMENTS elements as ${TAG}.*"
ids=()
previous=
for ((stage = 1; stage <= STAGES; stage++)); do
  args=(--parsable --job-name="${TAG}.${stage}" --array="1-${ELEMENTS}"
        --output=/dev/null --partition=debug)
  [[ -n $previous ]] && args+=(--dependency="aftercorr:${previous}")
  previous=$(sbatch "${args[@]}" "$WORK/stage.sh")
  ids+=("$previous")
done

printf 'waiting'
# `-j` takes job ids. `-n` takes job *names*, so the first clause used to be
# dead and the loop rested entirely on the tag match below it.
while squeue -h -j "$(IFS=,; echo "${ids[*]}")" -o '%i' 2>/dev/null | grep -q . ||
      squeue -h -o '%j' | grep -q "^${TAG}\."; do
  printf '.'
  sleep 5
done
echo

# sacct settles a few seconds behind the queue.
sleep 5

sacct -n -X -P -o "JobID,JobName,Start,End" -j "$(IFS=,; echo "${ids[*]}")" \
  | python3 -c '
import sys
from collections import defaultdict
from datetime import datetime

stages = defaultdict(lambda: {"start": [], "end": []})
for line in sys.stdin:
    parts = line.strip().split("|")
    if len(parts) != 4:
        continue
    job, name, start, end = parts
    if "_[" in job or start in ("Unknown", "None") or end in ("Unknown", "None"):
        continue
    stage = int(name.rsplit(".", 1)[1])
    when = datetime.fromisoformat
    stages[stage]["start"].append(when(start))
    stages[stage]["end"].append(when(end))

# The gap that matters: the last element of a stage finishing, and the first
# element of the next stage starting. aftercorr is elementwise, so measure per
# stage boundary rather than per element.
gaps = []
for stage in sorted(stages)[1:]:
    gap = (min(stages[stage]["start"]) - max(stages[stage - 1]["end"])).total_seconds()
    gaps.append((stage, gap))

if not gaps:
    print("no completed transitions; did the jobs run?")
    raise SystemExit(1)

for stage, gap in gaps:
    bar = "#" * min(60, int(gap))
    print(f"  stage {stage - 1} -> {stage}: {gap:6.1f}s {bar}")

values = sorted(g for _, g in gaps)
total = (max(stages[max(stages)]["end"]) - min(stages[1]["start"])).total_seconds()
print()
print(f"  transitions {len(values)}"
      f"   min {values[0]:.1f}s"
      f"   median {values[len(values)//2]:.1f}s"
      f"   max {values[-1]:.1f}s")
print(f"  deferred (>30s) {sum(1 for v in values if v > 30)} of {len(values)}")
print(f"  chain wall clock {total:.0f}s")
'
