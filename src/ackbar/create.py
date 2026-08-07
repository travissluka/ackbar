"""Materializing an experiment on disk.

After this runs, the experiment is addressed by *name* and the layer stack is
never read again. That is what "resolved once and frozen" means in practice: a
job eight hours from now reads `cfg/experiment.yaml`, not a tree of layer files
that may have been edited since.

Everything here is a one-time cost paid before any job exists, so it is the
right place to be thorough: validate all six steps, write the provenance, and
emit every cycle's job script up front.

`cfg/experiment.yaml` is the whole record of what an experiment is, and it is
the *finished* config: layers merged, observer bodies expanded, every `$(...)`
substituted. What is left in it is `{{...}}`, which is job time by definition
and cannot be resolved here.

The ordered layer files used to be copied in beside it, under `cfg/layers/`.
They are not any more. Nothing read them: `ackbar config why` replays the merge
over the layer tree in the checkout, not over a copy, and every other consumer
reads the merged config. They were a second, pre-substitution account of the
same thing, and two accounts of one config is how the wrong one gets read.
`provenance.json` still lists the layers by name and in order, with the commit
they were read at, which is what makes the copy reconstructable and made it
redundant.
"""

import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import yaml

from . import emit
from .config.jobtime import member_dir
from .graph.build import member_set
from .paths import Paths
from .validate import validate_experiment


class CreateError(Exception):
    pass


def create(config, site, schema, layers, *, root, force=False, python=None):
    """Validate, freeze, and emit. Returns (paths, graph, scripts)."""
    findings, graph, _ = validate_experiment(config, schema, site, root)
    if findings:
        raise CreateError(
            f"{len(findings)} problem(s) found by `ackbar validate`; nothing "
            f"was created. Run `ackbar validate` to see them."
        )

    paths = Paths.of(config, site)
    if paths.experiment_dir.exists() and not force:
        raise CreateError(
            f"{paths.experiment_dir} already exists. Pick another "
            f"experiment.name, or pass --force to overwrite an experiment that "
            f"has not been submitted."
        )
    if force and paths.experiment_dir.exists():
        _refuse_if_live(paths)
        shutil.rmtree(paths.experiment_dir)
        shutil.rmtree(paths.scratch_dir, ignore_errors=True)

    paths.ensure()
    paths.frozen_config.write_text(
        yaml.safe_dump(config, sort_keys=False, default_flow_style=False)
    )
    _freeze_templates(paths, root)
    _provenance(paths, config, site, root, layers)

    scripts = emit.write_all(config, paths, graph, root=root, python=python)

    if config["model"]["name"] == "stub":
        _stub_initial_condition(config, paths)
    elif config["model"].get("initial_condition"):
        _initial_condition(config, paths)
    return paths, graph, scripts


def _refuse_if_live(paths):
    """`--force` overwrites a mistake, never a running experiment."""
    if paths.ledger_file.exists() and paths.ledger_file.stat().st_size:
        raise CreateError(
            f"{paths.ledger_file} is not empty, so this experiment has already "
            f"submitted jobs. Cancel it and remove the directory by hand if "
            f"that is really what you want."
        )


def _freeze_templates(paths, root):
    """The SOCA document templates, copied out of the checkout.

    These are the one thing under `cfg/` that is *not* finished, and it is not
    an oversight. Their `$(UPPERCASE)` slots are task time: `$(BACKGROUND_DIR)`,
    `$(SLOT)` and `$(WINDOW_BEGIN)` vary by cycle, `$(MEMBER_BACKGROUNDS)` by
    member, and `$(OBSERVERS)` is not known until `stage.obs` has looked at the
    archive and seen which platforms have a file for that window. Filling them
    at create time would mean deciding at create time which observers a cycle
    six weeks out will have, which is the decision `observations.realize` exists
    to make and record.

    So these stay templates, and each task writes the document it actually ran
    beside its log. That file is the finished one.

    Copied rather than referenced, so that editing `config/soca/` in the
    checkout while an experiment is cycling cannot give cycle 40 a different
    document than cycle 39.
    """
    source = Path(root) / "config" / "soca"
    if not source.is_dir():
        raise CreateError(
            f"{source} does not exist, so no SOCA application has a document to "
            f"build from. It is part of the checkout, beside `config/layers`."
        )
    target = paths.templates
    target.mkdir(parents=True, exist_ok=True)
    for entry in sorted(source.glob("*.yaml")):
        shutil.copyfile(entry, target / entry.name)


def _provenance(paths, config, site, root, layers):
    """What produced this experiment, recorded where the outputs are."""
    paths.provenance.write_text(json.dumps({
        "experiment": config["experiment"]["name"],
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "ackbar_root": str(root),
        "ackbar_commit": _git_describe(root),
        "site": site.get("site", ""),
        "layers": [layer.name for layer in layers],
    }, indent=2) + "\n")


def _git_describe(root):
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        capture_output=True, text=True,
    )
    if result.returncode:
        return ""
    dirty = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain"],
        capture_output=True, text=True,
    )
    return result.stdout.strip() + ("-dirty" if dirty.stdout.strip() else "")


def _initial_condition(config, paths):
    """Cycle 0's restart set, symlinked from the offline initial condition.

    This is the one step that makes cycle 1 an ordinary cycle: `forecast(1)`
    reads `rst/0` by the same rule `forecast(50)` reads `rst/49`, so nothing in
    the graph, the model, or healing needs a notion of a first cycle.

    Links rather than copies. An initial condition is a read-only offline
    product of gigabytes, and copying it costs the ensemble size in disk for no
    gain: nothing in the experiment writes into `rst/0`.

    Two sources, because the control and the ensemble start from different
    states. The control is `model.initial_condition`, unperturbed, and it is
    what a deterministic experiment has. The members come from
    `ensemble.initial_condition`, one restart set each, which is what
    `tools/ensemble-ic.sh` produces. Without that key every member starts from
    the same state, which is an ensemble of zero spread; that is right for an
    experiment whose spread arrives from elsewhere and is a filter with nothing
    to work with for an LETKF, so it is said rather than defaulted into.
    """
    control = _restart_set(config["model"]["initial_condition"],
                           "model.initial_condition")
    ensemble = (config.get("ensemble") or {}).get("initial_condition")

    for member in member_set(config):
        source = control
        if ensemble and member != 0:
            source = _restart_set(
                Path(ensemble) / member_dir(member),
                f"ensemble.initial_condition, member {member}")
        target = paths.member_out("rst", 0, member)
        target.mkdir(parents=True, exist_ok=True)
        for entry in source:
            link = target / entry.name
            if link.is_symlink() or link.exists():
                link.unlink()
            link.symlink_to(entry)


def _restart_set(path, named):
    """The files of one restart set, or a refusal naming the key that is wrong."""
    source = Path(path)
    entries = sorted(source.iterdir()) if source.is_dir() else []
    if not entries:
        raise CreateError(
            f"{named} is {source}, which is empty or not a directory. "
            f"Experiments never generate their own inputs, so this has to be a "
            f"restart set an offline stage already produced."
        )
    return entries


def _stub_initial_condition(config, paths):
    """Cycle 0's restart set, so cycle 1 is an ordinary cycle.

    For a real model this is an offline product: a spun-up restart materialized
    into the experiment's own cycle-0 output location by a separate stage, and
    experiments never generate their own inputs. The stub has no offline stage
    to draw from, so its fake IC is written here rather than pretending an
    archive exists.
    """
    payload = b"ackbar stub initial condition\n"
    payload += b"\0" * int(config["model"]["stub"].get("bytes", 0))
    for member in member_set(config):
        target = paths.member_out("rst", 0, member) / "restart.stub"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
