#!/usr/bin/env python3
"""Write the perturbation document, and turn what it produces into restart sets.

    tools/ensemble-ic.py plan  <layer> <static> <levels> <metadata> <restart> \\
                              <members> <out.yaml>
    tools/ensemble-ic.py place <config> <restart-dir> <states-dir> <target>

Called by `tools/ensemble-ic.sh` and not otherwise useful on its own. The two
verbs are in one file so that both halves agree about which member is which.

`plan` builds the `soca_enspert.x` document. Its background error is lifted out
of `config/layers/da/variational.yaml` whole, unlike the dirac test's, which
takes the central block alone. That difference is the point: a dirac measures a
correlation, so the standard deviations would spoil the reading, while a
perturbation *is* a draw from the covariance and the standard deviations are
exactly what sets its size. An ensemble perturbed by a correlation with no
amplitude is an ensemble of the wrong spread, and nothing downstream says so.

`place` turns each perturbed state into a restart set, which is the same
operation `writeback` performs on an analysis and is done by calling it. A
member's initial condition has to be a whole restart set, not a state: the
forecast reads sea ice, icebergs and a coupler clock too.
"""

import os
import re
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

#: The date the perturbation is generated at. It is not the date the ensemble is
#: valid at, and nothing downstream reads it: `place` copies the initial
#: condition's own `coupler.res`, which is what says when the state is. Fixed so
#: that the document is reproducible.
DATE = "2000-01-01T00:00:00Z"
DATADIR = "out"
EXPERIMENT = "pert"

#: What is perturbed, and what the state has to carry, both read out of the
#: variational layer rather than restated here.
#:
#: They are different lists and the difference matters. `analysis variables` is
#: what a draw from B is a draw *of*; `background variables` is what the B
#: blocks have to be able to read, and `BalanceSOCA` refuses to construct
#: without `sea_water_cell_thickness` among them. A state carrying only the
#: perturbed fields aborts inside the linear variable change, several blocks in.
PERTURBED = "analysis variables"
CARRIED = "background variables"


def _solver(layer):
    return yaml.safe_load(Path(layer).read_text())["solver"]


def main(argv):
    if len(argv) < 2:
        sys.exit(__doc__.strip().splitlines()[2].strip())
    verb, rest = argv[1], argv[2:]
    if verb == "plan":
        return plan(rest)
    if verb == "place":
        return place(rest)
    sys.exit(f"ensemble-ic: unknown verb {verb!r}; the verbs are plan and place")


# ------------------------------------------------------------------------------
# plan

def plan(argv):
    layer, static, levels, metadata, restart, members, out = argv[0:7]
    solver = _solver(layer)
    perturbed = list(solver[PERTURBED])
    carried = list(solver[CARRIED])
    document = {
        "geometry": {
            "geom_grid_file": "soca_gridspec.nc",
            "mom6_input_nml": "mom_input.nml",
            "fields metadata": metadata,
        },
        # The identity model, because the perturbation is drawn at one instant
        # and not integrated. Perturbing and then running the model forward is
        # the better ensemble and it is a different stage: this one exists to
        # give cycle 0 a spread at all, and the first cycle's forecast is what
        # makes that spread dynamically consistent.
        "model": {
            "name": "Identity",
            "tstep": "PT6H",
            "model variables": carried,
        },
        "initial condition": {
            "read_from_file": 1,
            "basename": os.path.dirname(restart) + os.sep,
            "ocn_filename": os.path.basename(restart),
            "date": DATE,
            "state variables": carried,
        },
        "background error": _covariance(layer, static, levels),
        "members": int(members),
        "perturbed variables": perturbed,
        "forecast length": "PT0H",
        "output": {"frequency": "PT6H", "datadir": DATADIR, "date": DATE,
                   "exp": EXPERIMENT, "type": "ens", "date colons": False},
    }
    Path(out).write_text(yaml.safe_dump(document, sort_keys=False))
    print(f"ensemble-ic: wrote {out}")
    return 0


def _covariance(layer, static, levels):
    """The experiment's whole background error, resolved.

    Whole, including the outer blocks: `SOCAParametricOceanStdDev` is what turns
    a correlation into a covariance, and a draw from a correlation has no
    amplitude. The blocks are also read in the order they appear, which is why
    this passes the section through rather than rebuilding it.

    Through `soca.background_error`, which is what fills in the variable lists
    the layer deliberately does not state. Without them the balance operator
    produces an increment carrying no fields, and every member comes back
    identical to the state it was perturbed from.
    """
    from ackbar.soca import background_error

    solver = _solver(layer)
    return _substitute(background_error(solver, list(solver[PERTURBED])),
                       {"domain_static": static, "diffusion_levels": levels})


def _substitute(node, values):
    """Resolve the experiment-time tokens a background error block can contain.

    Only those two. Anything else surviving is a token this does not know how to
    resolve, and it fails rather than handing `$(...)` to eckit, which would
    read it as a filename.
    """
    if isinstance(node, dict):
        return {key: _substitute(value, values) for key, value in node.items()}
    if isinstance(node, list):
        return [_substitute(value, values) for value in node]
    if not isinstance(node, str):
        return node

    def one(match):
        if match.group(1) not in values:
            sys.exit(f"ensemble-ic: nothing here can resolve $({match.group(1)})")
        return str(values[match.group(1)])

    text = re.sub(r"\$\((\w+)\)", one, node)
    # `levels:` has to come back as an integer. eckit will not coerce it.
    return int(text) if text.isdigit() else text


# ------------------------------------------------------------------------------
# place

def place(argv):
    """Each perturbed state, written into its own copy of the restart set."""
    from ackbar import writeback
    from ackbar.config.jobtime import member_dir
    from ackbar.mom6sis2 import STAMP, ModelError

    config_path, source, states, target = argv[0:4]
    config = _config(config_path)
    source, states, target = Path(source), Path(states), Path(target)

    found = _by_member(states)
    if not found:
        sys.exit(f"ensemble-ic: {states} holds no perturbed states; "
                 f"soca_enspert.x exited 0 and wrote nothing")

    target.mkdir(parents=True, exist_ok=True)
    for member, state in sorted(found.items()):
        member_target = target / member_dir(member)
        member_target.mkdir(parents=True, exist_ok=True)
        writeback.copy_set(source, member_target, exclude=(STAMP,))
        try:
            lines = writeback.apply_analysis(
                config, state, member_target / config["model"]["restart"]["ocn"])
        except ModelError as error:
            sys.exit(f"ensemble-ic: {member_dir(member)}: {error}")
        # `coupler.res` last, so a set that has one is a set that is whole.
        (member_target / STAMP).write_bytes((source / STAMP).read_bytes())
        print(f"ensemble-ic: {member_dir(member)}")
        for line in lines:
            print(f"ensemble-ic:   {line}")
    return 0


def _by_member(states):
    """Which perturbed state is which member.

    `soca_enspert.x` writes `ocn.<exp>.ens.<member>.<date>.<offset>.nc`, and the
    member field is the one that matters here. Members are numbered from one,
    which is also where ACKBAR's ensemble starts: member zero is the control and
    it is not perturbed, because the point of a control is that it is the state
    everything else is a perturbation of.
    """
    found = {}
    for path in sorted(Path(states).glob(f"ocn.{EXPERIMENT}.ens.*.nc")):
        fields = path.name.split(".")
        if len(fields) > 4 and fields[3].isdigit():
            found[int(fields[3])] = path
    return found


def _config(path):
    """Enough of an experiment configuration for `writeback` to read.

    Built here rather than resolved from a layer stack, because this stage has
    no experiment: it is keyed on domain, like the gridspec and the background
    error, and shared by every experiment on that domain.
    """
    values = yaml.safe_load(Path(path).read_text())
    return {
        "model": {"name": "mom6sis2",
                  "restart": {"ocn": "MOM.res.nc"},
                  "fields metadata": values["metadata"]},
        "solver": {"analysis variables": list(_solver(values["layer"])[PERTURBED])},
        "domain": {"name": values["domain"], "static": values["static"]},
    }


if __name__ == "__main__":
    sys.exit(main(sys.argv))
