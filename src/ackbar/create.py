"""Materializing an experiment on disk.

After this runs, the experiment is addressed by *name* and the layer stack is
never read again. That is what "resolved once and frozen" means in practice: a
job eight hours from now reads `cfg/experiment.yaml`, not a tree of layer files
that may have been edited since.

Everything here is a one-time cost paid before any job exists, so it is the
right place to be thorough: validate all six steps, write the provenance, and
emit every cycle's job script up front.
"""

import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import yaml

from . import emit
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
    _freeze_layers(paths, layers)
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


def _freeze_layers(paths, layers):
    """The ordered layer files, verbatim and numbered.

    Verbatim rather than merged, because "which file said this" is the question
    asked when a result looks wrong, and the merged config has already thrown
    that away. Numbered because merge order is the whole semantics.
    """
    target = paths.sub("cfg") / "layers"
    target.mkdir(parents=True, exist_ok=True)
    for index, layer in enumerate(layers, start=1):
        safe = layer.name.replace("/", "_")
        shutil.copyfile(layer.path, target / f"{index:02d}-{safe}.yaml")


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

    Links rather than copies. The initial condition is a read-only offline
    product of gigabytes, and copying it per member would multiply it by the
    ensemble size to no end: every member starts from the same state, and what
    makes them differ is perturbation or an ensemble source, neither of which is
    a property of this directory.
    """
    source = Path(config["model"]["initial_condition"])
    entries = sorted(source.iterdir()) if source.is_dir() else []
    if not entries:
        raise CreateError(
            f"model.initial_condition {source} is empty or not a directory. "
            f"Experiments never generate their own inputs, so this has to be a "
            f"restart set an offline stage already produced."
        )
    for member in member_set(config):
        target = paths.member_out("rst", 0, member)
        target.mkdir(parents=True, exist_ok=True)
        for entry in entries:
            link = target / entry.name
            if link.is_symlink() or link.exists():
                link.unlink()
            link.symlink_to(entry)


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
