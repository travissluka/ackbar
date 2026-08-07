#!/bin/bash
# Cold start a domain and promote the restarts it writes to the IC stage.
#
#   tools/coldstart-ic.sh <domain> <YYYY-MM-DDThh> <hours> <source-slug>
#   tools/coldstart-ic.sh gom_25km 2015-01-04T12 12 hycom-smoke
#
# Writes $ACKBAR_STATIC_ROOT/ic/<domain>/<source-slug>/<YYYYmmddThh>, which is
# what an experiment's `model.initial_condition` names.
#
# **Choose the start hour so that start + hours lands on 00, 06, 12 or 18Z.**
# That is not a preference, it is the only chance anyone gets to choose it. The
# product is named for the end of the integration, an experiment's `cycle.start`
# has to equal its initial condition's valid time, and every analysis time after
# that is `start + n * cycle.length`. So the hour picked here is the hour every
# experiment built on this initial condition cycles on, for as long as it exists.
#
# The example above used to read `2015-01-04T13`, which is why the smoke initial
# condition is valid at 01:00 and every tier 3 experiment cycles at 01Z and 13Z.
# Analysis times are synoptic everywhere in operational and reanalysis practice,
# and observation archives are binned on them.
#
# Cycling needs a restart set to start from, and a domain that initializes from
# z-level temperature and salinity does not have one: MOM6 builds its state from
# `ic.nc` during initialization and only writes a restart at the end of a run.
# So the first restart set for a new domain has to be made by running the model
# once, which is this.
#
# The date is the one the domain's own `ic.nc` and `obc.nc` are valid at, and it
# is an argument rather than something read out of the files because promoting a
# state to a date is the decision this stage exists to record. Getting it wrong
# produces a restart set that is internally consistent and simply not when it
# says it is, which nothing downstream can detect.
#
# `<hours>` is short on purpose. This is a smoke start, not a spinup: enough
# integration for the model to have written a self-consistent restart set, not
# enough for anything to have equilibrated. The slug is what warns readers,
# since an experiment selects a state by writing this path out and that is the
# last point anybody looks at it. Name a real spinup differently.
set -euo pipefail

ACKBAR_ROOT=$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)
source "$ACKBAR_ROOT/site/activate.sh"

DOMAIN=${1:?usage: coldstart-ic.sh <domain> <YYYY-MM-DDThh> <hours> <source-slug>}
WHEN=${2:?usage: coldstart-ic.sh <domain> <YYYY-MM-DDThh> <hours> <source-slug>}
HOURS=${3:?usage: coldstart-ic.sh <domain> <YYYY-MM-DDThh> <hours> <source-slug>}
SLUG=${4:?usage: coldstart-ic.sh <domain> <YYYY-MM-DDThh> <hours> <source-slug>}

: "${ACKBAR_STATIC_ROOT:?the site did not set ACKBAR_STATIC_ROOT}"

# How many ranks is a property of the machine, so the site file owns it and
# nothing here may name a number. Overridable per invocation because this runs
# outside Slurm and nothing schedules it against what else is on the box.
NTASKS=${NTASKS:-${ACKBAR_MPI_TASKS:?the site did not set ACKBAR_MPI_TASKS}}

# Read out of the domain layer, so this integrates the same configuration a
# cycling forecast would.
source "$ACKBAR_ROOT/tools/domain-paths.sh"
domain_paths "$DOMAIN"
EXE=$ACKBAR_ROOT/pkg/mom6sis2/ice_ocean_SIS2/build/coupler_main

read -r YY MM DD HH <<< "$(date -u -d "${WHEN}:00:00Z" '+%Y %-m %-d %-H')"

# The state is valid at the *end* of the integration, not at its start, so that
# is what names the directory. A restart set stamped with the date the model
# started from is internally consistent and simply not when it says it is, and
# nothing downstream can detect that: every cycle would run with its window
# offset from its background by the length of this cold start.
#
# Epoch arithmetic rather than `date -d "<stamp> + N hours"`. Given a timestamp
# with no zone marker, GNU date reads the `+ N` as a UTC offset instead of an
# interval, so "2015-01-04 13:00:00 + 12 hours" is 02:00 on the 4th rather than
# 01:00 on the 5th. It is a silent wrong answer of exactly the kind this stamp
# exists to prevent.
BEGIN=$(date -u -d "${WHEN}:00:00Z" +%s)
VALID=$(date -u -d "@$((BEGIN + HOURS * 3600))" +%Y-%m-%dT%H)
STAMP=$(date -u -d "@$((BEGIN + HOURS * 3600))" +%Y%m%dT%H)
OUT=$ACKBAR_STATIC_ROOT/ic/$DOMAIN/$SLUG/$STAMP

for path in "$EXE" "$BASE/MOM_input" "$OVERRIDE/MOM_override" \
            "$OVERRIDE/SIS_override" "$DATA"; do
    [[ -e $path ]] || { echo "coldstart-ic: $path does not exist" >&2; exit 1; }
done
[[ -e $OUT ]] && { echo "coldstart-ic: $OUT already exists; remove it to rebuild" >&2; exit 1; }

WORK=$(mktemp -d "${TMPDIR:-/tmp}/coldstart-ic.XXXXXX")
trap 'rm -rf "$WORK"' EXIT
cd "$WORK"
mkdir INPUT RESTART
for path in "$DATA"/*; do ln -s "$(readlink -f "$path")" "INPUT/$(basename "$path")"; done
for name in data_table field_table MOM_input SIS_input; do cp "$BASE/$name" .; done
cp "$OVERRIDE/MOM_override" "$OVERRIDE/SIS_override" .
ln -s "$EXE" coupler_main
printf 'coldstart\n%s %s %s %s 0 0\n' "$YY" "$MM" "$DD" "$HH" > diag_table

# The same `input.nml` edits ACKBAR makes for a forecast: run length, start
# date, and the parameter file list that makes the override get read at all.
# Written here rather than imported from the workflow because this runs before
# an experiment exists to configure.
python3 - "$BASE/input.nml" "$HOURS" "$YY,$MM,$DD,$HH,0,0" <<'EOF' > input.nml
import re, sys
text, hours, start = open(sys.argv[1]).read(), sys.argv[2], sys.argv[3]

def group(name, edits):
    global text
    def patch(match):
        body = match.group(0)
        for key, value in edits.items():
            pattern = rf"^(\s*){key}\s*=[^\n]*(?:\n(?!\s*(?:\w+\s*=|/|$))[^\n]*)*"
            if re.search(pattern, body, re.M):
                body = re.sub(pattern, rf"\g<1>{key} = {value},", body, flags=re.M)
            else:
                body = body.rstrip()[:-1].rstrip() + f"\n  {key} = {value},\n/"
        return body
    text = re.sub(rf"&{name}\b.*?^\s*/\s*$", patch, text, flags=re.S | re.M)

group("coupler_nml", {"months": 0, "days": 0, "hours": hours,
                      "current_date": start, "force_date_from_namelist": ".true."})
group("MOM_input_nml", {"parameter_filename": "'MOM_input', 'MOM_override'"})
group("SIS_input_nml", {"parameter_filename": "'SIS_input', 'SIS_override'"})
sys.stdout.write(text)
EOF

echo "coldstart-ic: integrating $DOMAIN for ${HOURS}h from $WHEN on $NTASKS PEs"
mpiexec -n "$NTASKS" ./coupler_main > model.log 2>&1 || {
    echo "coldstart-ic: the model failed; log follows" >&2
    tail -40 model.log >&2
    exit 1
}

[[ -s RESTART/coupler.res ]] || {
    echo "coldstart-ic: the model exited 0 and wrote no RESTART/coupler.res, so there is no restart set" >&2
    exit 1
}

mkdir -p "$(dirname "$OUT")"
mv RESTART "$OUT"
cat > "$(dirname "$OUT")/README" <<EOF
$DOMAIN initial conditions, $SLUG. One state per directory, named for the date
it is valid at: $STAMP is ${VALID}:00:00Z. That is the *end* of the cold start,
which began at ${WHEN}:00:00Z, the date the domain's own ic.nc and obc.nc carry.

Not spun up. This is a ${HOURS} hour cold start written by
tools/coldstart-ic.sh, from whatever the domain's own ic.nc holds, which for the
Gulf domains is a single SODA 3.3.1 five-day mean. It exists so that cycling can
be developed and tested against a real restart set. It is not a state to draw a
scientific conclusion from, and an experiment that matters wants a real spinup
here instead under its own slug.

Reproduce with:
    tools/coldstart-ic.sh $DOMAIN $WHEN $HOURS $SLUG
EOF

echo "coldstart-ic: wrote $OUT"
ls "$OUT"
