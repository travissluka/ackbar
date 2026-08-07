#!/bin/bash
# Promote a free run's states to a read-only truth archive.
#
#   tools/promote-truth.sh <experiment> <archive-name>
#   tools/promote-truth.sh osse-truth osse-2015
#
# Writes $ACKBAR_STATIC_ROOT/truth/<domain>/<archive-name>/<YYYYmmddTHHMM>/,
# one directory per state, each holding the restart set under its ordinary
# names. That is the same layout as `ic/<domain>/<product>/<date>/`, so a truth
# state is addressed exactly the way an initial condition is and `--truth-run`
# in the observation generator needs no second reader.
#
# **Keyed by the date the state is valid at, which is not the directory it came
# from.** `run/<T>/rst/` holds what cycle T's *forecast* wrote, so it is valid at
# T plus one cycle; `run/<T>/slot/mem000/<S>/` is already named for its own
# valid time and is copied across unchanged. Getting this wrong produces an
# archive that is internally consistent and off by one cycle everywhere, which
# nothing downstream can detect: every departure would simply be larger than it
# should be, uniformly, which reads as a worse forecast rather than as a bug.
#
# Copies rather than symlinks or a move. The point of promotion is that the
# archive outlives the experiment: `cleanup` reaps `run/` on a schedule, and an
# archive of links into a reaped directory is an archive of dangling links. Once
# this has run the truth experiment's own output can be deleted.
#
# Idempotent by date: a state already in the archive is left alone and reported,
# so a run that was extended can be promoted again without re-copying what is
# already there.
set -euo pipefail

ACKBAR_ROOT=$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)
source "$ACKBAR_ROOT/site/activate.sh"

EXPERIMENT=${1:?usage: promote-truth.sh <experiment> <archive-name>}
ARCHIVE=${2:?usage: promote-truth.sh <experiment> <archive-name>}

: "${ACKBAR_STATIC_ROOT:?the site did not set ACKBAR_STATIC_ROOT}"
: "${ACKBAR_OUTPUT_ROOT:?the site did not set ACKBAR_OUTPUT_ROOT}"

EXP_DIR=$ACKBAR_OUTPUT_ROOT/$EXPERIMENT
CFG=$EXP_DIR/cfg/experiment.yaml
[[ -f $CFG ]] || { echo "promote-truth: no such experiment: $CFG" >&2; exit 1; }

# The domain and the cycle length come out of the frozen config rather than the
# command line, because both are properties of the experiment being promoted and
# a second spelling of the cycle length is a second chance to shift every date.
read -r DOMAIN LENGTH SOLVER < <("$ACKBAR_ROOT/.venv/bin/python" - "$CFG" <<'EOF'
import sys, yaml
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
config = yaml.safe_load(open(sys.argv[1]))
print(config["domain"]["name"], config["cycle"]["length"],
      config["solver"]["name"])
EOF
)

# A truth run is a free run. An experiment with an analysis has states that are
# not a trajectory of the model alone, and promoting one as truth would score
# every experiment against something that had already seen the observations.
if [[ $SOLVER != none ]]; then
    echo "promote-truth: $EXPERIMENT has solver '$SOLVER', not 'none'." >&2
    echo "promote-truth: a truth run must be a free run; its states are what" >&2
    echo "promote-truth: every experiment is scored against." >&2
    exit 1
fi

OUT=$ACKBAR_STATIC_ROOT/truth/$DOMAIN/$ARCHIVE
mkdir -p "$OUT"

copied=0
skipped=0

promote() {   # <source dir> <valid time, as YYYYmmddTHHMM>
    local source=$1 stamp=$2
    if [[ -d $OUT/$stamp ]]; then
        skipped=$((skipped + 1))
        return
    fi
    [[ -f $source/MOM.res.nc ]] || return
    # Into a temporary name and then renamed, so that an interrupted copy never
    # leaves a half-written state behind under a date that looks promoted.
    #
    # `-L` is not optional. A cycle's restart directory is usually real files,
    # but cycle 0 is the *materialized initial condition*, which `ackbar create`
    # writes as symlinks into whatever offline product the experiment named. A
    # plain `cp -r` preserves those, and the archive then holds links into a
    # directory `cleanup` is entitled to delete: dangling links at best, and at
    # worst a later write through one of them reaching back and modifying the
    # original. Which is exactly what happened the first time this ran.
    rm -rf "$OUT/.$stamp.partial"
    cp -rL "$source" "$OUT/.$stamp.partial"
    mv "$OUT/.$stamp.partial" "$OUT/$stamp"
    copied=$((copied + 1))
}

for cycle_dir in "$EXP_DIR"/run/*/; do
    date=$(basename "$cycle_dir")
    [[ $date =~ ^[0-9]{8}T[0-9]{6}Z$ ]] || continue

    # The restart set, valid one cycle after the directory that holds it.
    if [[ -d $cycle_dir/rst/mem000 ]]; then
        valid=$("$ACKBAR_ROOT/.venv/bin/python" - "$date" "$LENGTH" <<'EOF'
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from ackbar.duration import parse_duration, parse_instant
when = parse_instant(sys.argv[1]) + parse_duration(sys.argv[2])
print(when.strftime("%Y%m%dT%H%M"))
EOF
)
        promote "$cycle_dir/rst/mem000" "$valid"
    fi

    # The sub-window states, each already named for when it is valid.
    for slot_dir in "$cycle_dir"/slot/mem000/*/; do
        [[ -d $slot_dir ]] || continue
        slot=$(basename "$slot_dir")
        [[ $slot =~ ^[0-9]{8}T[0-9]{6}Z$ ]] || continue
        promote "$slot_dir" "$(date -u -d "${slot:0:8} ${slot:9:2}:${slot:11:2}" +%Y%m%dT%H%M)"
    done
done

cat > "$OUT/README" <<EOF
Truth archive: $ARCHIVE, on $DOMAIN, promoted from the free run '$EXPERIMENT'.

One directory per state, named for the instant it is valid at. Read-only: it is
what every experiment on this period is scored against, so a state that changes
after an experiment has been verified against it invalidates that verification
silently.

The cycle length of the run that produced it was $LENGTH, and any sub-window
states it wrote are here at their own cadence.

**The states are not all the same shape.** One per cycle length is a complete
restart set (MOM.res.nc, ice_model.res.nc, coupler.res), because it came from a
cycle's restart directory. The sub-window states between them hold MOM.res.nc
alone, because that is all a forecast dumps mid-integration. Everything that
reads a truth state for its ocean fields is unaffected; anything that wants to
*start a model* from one needs the complete kind, and tools/restamp-ic.sh
refuses the other with a message saying so.

Regenerate with:
    tools/promote-truth.sh $EXPERIMENT $ARCHIVE
EOF

echo "promote-truth: $copied state(s) copied, $skipped already present"
echo "promote-truth: $OUT"
ls "$OUT" | head -5
