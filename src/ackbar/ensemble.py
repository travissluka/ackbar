"""Which members an ensemble cycle actually has, and what to do about the rest.

A member's forecast can be missing for reasons that have nothing to do with the
experiment: a node died, a job hit its time limit, the queue killed an array
element. Once members are array elements this stops being rare, because an
experiment with twenty of them has twenty chances of it per cycle, and the
question "what should the analysis do" has no default answer that is right for
everyone.

So it is stated per experiment, as `ensemble.on_missing_member`, and the three
values are three different experiments rather than three degrees of tolerance:

    fail_cycle          the ensemble is the experiment. Stop.
    run_degraded        assimilate what arrived, and record what did not.
    replace_from_mean   rebuild the missing member from the others, and keep
                        the ensemble at the size it was configured with.

`run_degraded` is the one that needs care. A filter given eighteen members where
it expected twenty produces an analysis of lower rank, more sampling noise and
less spread, and the effect persists after the missing member comes back. That
is a fact about the cycle, so it is written into the cycle's own record rather
than left in a log: two experiments that differ in which members ran are not
comparable, and nothing else would say so.

`replace_from_mean` keeps the rank the same in name only, since the replacement
carries no independent information: the ensemble spread is what it was, spread
over one more member. What it buys is that the member exists, so its forecast
runs, and the ensemble is back to full strength one cycle later rather than
carrying a hole forever. A mean state is a state a model integrates: it is the
same object the LETKF hands the control member every cycle.
"""

import json

import netCDF4
import numpy as np

from .config.jobtime import member_dir
from .mom6sis2 import STAMP, ModelError
from .writeback import analysed_fields, copy_set, masks_of, place

#: Every value `ensemble.on_missing_member` may take. Stated here as well as in
#: the schema, because this module is what makes each of them mean something and
#: a value the schema allows and this does not is a cycle that fails on a policy
#: nobody implemented.
POLICIES = ("fail_cycle", "run_degraded", "replace_from_mean")

#: What ACKBAR calls the record of which members a cycle had. Beside the
#: analysis rather than under `cfg/`, for the same reason the realized observer
#: list is: it is a product of the cycle and not of the configuration.
LEDGER = "members.json"


def ensemble_members(config, members):
    """The members that make up the ensemble, which is every one but the control.

    The control is `mem000` and it is inside the same member array as the rest,
    not a parallel concept. What makes it different is only that an ensemble
    filter does not assimilate it: its analysis is the posterior mean, which the
    filter computes anyway.
    """
    return tuple(member for member in members if member != 0)


def available(paths, cycle, members, stamp=STAMP):
    """Which members have a complete background restart set for this cycle."""
    return tuple(member for member in members
                 if (paths.member_out("rst", cycle - 1, member) / stamp).exists())


def resolve(config, paths, cycle, members, stamp=STAMP):
    """Decide which members this cycle assimilates, applying the policy.

    Returns the member list. Writes the record either way, including when
    nothing was missing, because "every member was there" is a fact worth having
    in the same place and the same shape as "two were not".
    """
    policy = (config.get("ensemble") or {}).get("on_missing_member", "fail_cycle")
    if policy not in POLICIES:
        raise ModelError(
            f"ensemble.on_missing_member is {policy!r}; the policies are "
            f"{', '.join(POLICIES)}"
        )

    present = available(paths, cycle, members, stamp)
    missing = tuple(member for member in members if member not in present)
    if not missing:
        _record(paths, cycle, policy, present, (), ())
        return present

    named = ", ".join(member_dir(member) for member in missing)
    if policy == "fail_cycle":
        raise ModelError(
            f"{len(missing)} of {len(members)} ensemble member(s) have no "
            f"background for cycle {cycle}: {named}. The experiment's "
            f"`ensemble.on_missing_member` policy is fail_cycle."
        )

    if policy == "run_degraded":
        print(f"ackbar: cycle {cycle} is assimilating {len(present)} of "
              f"{len(members)} member(s); {named} have no background")
        _record(paths, cycle, policy, present, missing, ())
        return present

    rebuilt = tuple(_replace(config, paths, cycle, member, present, stamp)
                    for member in missing)
    print(f"ackbar: cycle {cycle} rebuilt {len(rebuilt)} member(s) from the "
          f"mean of the {len(present)} that arrived: {named}")
    _record(paths, cycle, policy, present, missing, rebuilt)
    return tuple(sorted(present + rebuilt))


def _record(paths, cycle, policy, present, missing, rebuilt):
    """The cycle's own account of its ensemble.

    Written next to the analysis so that a comparison between two experiments
    can find it without knowing how either of them was run.
    """
    target = paths.cycle_out("ana", cycle) / LEDGER
    payload = {
        "cycle": cycle,
        "policy": policy,
        "assimilated": sorted(present) + sorted(rebuilt),
        "present": sorted(present),
        "missing": sorted(missing),
        "rebuilt": sorted(rebuilt),
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + ".partial")
    temp.write_text(json.dumps(payload, indent=2) + "\n")
    temp.replace(target)
    return target


def read(paths, cycle):
    """The record `resolve` wrote, or None if this cycle has no ensemble."""
    target = paths.cycle_out("ana", cycle) / LEDGER
    if not target.exists():
        return None
    return json.loads(target.read_text())


# --- rebuilding a member ------------------------------------------------------

def _replace(config, paths, cycle, member, present, stamp):
    """Build *member*'s missing background from the mean of *present*.

    A copy of one arriving member's restart set with the analysis variables
    replaced by the ensemble mean of them. One member's set rather than a fresh
    file, for the same reason writeback copies the background: the restart
    carries far more than the analysis variables, and every one of those fields
    has to be something the model can integrate.

    Only the analysis variables are averaged, and that is the conservative
    choice rather than the complete one. Averaging layer thicknesses across
    members gives a column no member had, and the temperature being written on
    to it was computed on a different one.
    """
    if not present:
        raise ModelError(
            f"no member of cycle {cycle}'s ensemble arrived, so there is "
            f"nothing to build a mean from"
        )

    restart = (config["model"].get("restart") or {})["ocn"]
    source = paths.member_out("rst", cycle - 1, present[0])
    target = paths.member_out("rst", cycle - 1, member)
    target.mkdir(parents=True, exist_ok=True)
    copy_set(source, target, exclude=(stamp,))

    masks = masks_of(config)
    with netCDF4.Dataset(target / restart, "r+") as rebuilt:
        rebuilt.set_auto_mask(False)
        for field in analysed_fields(config):
            mask = masks[field["grid"]]
            place(rebuilt, field, mask,
                  _mean(paths, cycle, present, restart, field["io name"],
                        mask.shape))

    # Last, so that a set that has one is a set that is whole.
    (target / stamp).write_bytes((source / stamp).read_bytes())
    return member


def _mean(paths, cycle, present, restart, io, shape):
    """One variable, averaged over the members that arrived.

    Accumulated one member at a time rather than stacked, because an ensemble is
    twenty full model states and holding them all at once to take a mean is how
    a one-node job runs out of memory doing arithmetic.

    Cropped to the tracer grid, which is the shape a staggered variable's
    analysis has and therefore the shape `writeback.place` writes. The face the
    tracer grid has no cell for keeps whatever the member this was copied from
    had, exactly as it does when an analysis is written.
    """
    total = None
    for other in present:
        path = paths.member_out("rst", cycle - 1, other) / restart
        with netCDF4.Dataset(path) as data:
            data.set_auto_mask(False)
            values = np.asarray(data.variables[io][0], dtype="f8")
        total = values if total is None else total + values
    return total[..., :shape[0], :shape[1]] / len(present)
