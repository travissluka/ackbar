"""The experiment-time substitution pass.

Two passes exist, and they are deliberately different syntaxes:

``$(name)``
    Experiment time. Resolved once, when the experiment is created, and frozen.
    Everything it can see is known before any job is submitted.

``{{name}}``
    Job time. Cycle date, window bounds, member index, forecast length. Cannot
    be known once, so this pass must leave it strictly alone.

v3 had exactly this split. What it lacked was a named, closed set of symbols, so
the second pass resolved whatever happened to be in scope. Here an unknown
symbol is an error that names the config path, and it is reported before
anything is submitted rather than by a job eight hours from now.

Substitution runs *after* the merge, never before. A layer has to be able to
override a value that another layer interpolated, and the concrete case is the
observer land mask: filter chains have no merge key so they replace wholesale,
which means the part that varies by DA mode has to be a substituted value.
"""

import re

TOKEN = re.compile(r"\$\(([^()]+)\)")
WHOLE = re.compile(r"^\$\(([^()]+)\)$")


class ResolveError(Exception):
    def __init__(self, message, path=""):
        self.message = message
        self.path = path
        super().__init__(f"{path}: {message}" if path else message)


def site_symbols(site):
    """The symbols derived from the site layer and nothing else."""
    return {
        "scratch_root": site["scratch_root"],
        "output_root": site["output_root"],
    }


def builtin_symbols(config, site):
    """The closed set of experiment-time symbols ACKBAR provides itself.

    Everything else a layer wants to reference must come from ``vars``, so that
    a typo is an unknown symbol rather than a silently empty string.
    """
    name = _require(config, ("experiment", "name"))
    experiment_dir = f"{site['output_root']}/{name}"
    symbols = site_symbols(site)
    symbols.update({
        "experiment": name,
        "experiment_dir": experiment_dir,
        "scratch_dir": f"{site['scratch_root']}/{name}",
    })
    # The on-disk layout, so that no layer spells these out and they cannot
    # disagree with what the workflow actually creates. This list is
    # `paths.SUBDIRS` and has to stay it; `tests/test_resolve.py` is what makes
    # that true, because importing `paths` from here would close a cycle
    # (`paths` imports `config.jobtime`).
    #
    # Only the top level. `ledger`, `stats`, `log`, `rst` and `done` are not
    # here because they are not experiment-time directories at all: they live
    # under `run/<date>/`, so there is no one path a layer could be given, and
    # publishing one meant publishing a directory nothing creates.
    for sub in ("cfg", "ana", "bkg", "corr_vt", "fcst", "obs_in", "obs_out", "run"):
        symbols[f"{sub}_dir"] = f"{experiment_dir}/{sub}"

    # The roots a layer needs in order to name a file that is not the
    # experiment's own: the checkout (executables, config templates), the model
    # dataset mirror, and what the offline stages produce. All three come from
    # the site file, which is the only place allowed to know a machine-specific
    # path, so a model layer can point at a grid file without becoming
    # machine-specific itself.
    #
    # Absent from the table rather than empty when the site does not set them,
    # so a layer that needs one fails with "unknown symbol" instead of resolving
    # to a path that begins at the root of the disk.
    for name, key in (("ackbar_root", "root"),
                      ("datasets_root", "datasets_root"),
                      ("static_root", "static_root")):
        if site.get(key):
            symbols[name] = site[key]
    return symbols


def symbol_table(config, site):
    """Built-ins plus the experiment's own ``vars``, fully resolved.

    A var may reference another var or a built-in. A var may not shadow a
    built-in, because that makes the meaning of a symbol depend on which layer
    happened to define it.
    """
    builtins = builtin_symbols(config, site)
    declared = config.get("vars") or {}

    shadowed = sorted(set(declared) & set(builtins))
    if shadowed:
        raise ResolveError(
            f"vars may not redefine built-in symbols: {', '.join(shadowed)}",
            "vars",
        )

    table = dict(builtins)
    for name in declared:
        _expand_symbol(name, declared, table, ())
    return table


def _expand_symbol(name, declared, table, visiting):
    if name in table:
        return table[name]
    if name in visiting:
        chain = " -> ".join(visiting + (name,))
        raise ResolveError(f"circular reference: {chain}", "vars")
    if name not in declared:
        raise ResolveError(f"unknown symbol $({name})", "vars")

    value = _substitute(
        declared[name],
        lambda ref: _expand_symbol(ref, declared, table, visiting + (name,)),
        f"vars.{name}",
    )
    table[name] = value
    return value


def resolve(config, site):
    """Return the config with every ``$(...)`` replaced. ``{{...}}`` survives."""
    table = symbol_table(config, site)
    return _walk(config, table, ())


def _walk(node, table, path):
    if isinstance(node, dict):
        return {k: _walk(v, table, path + (k,)) for k, v in node.items()}
    if isinstance(node, list):
        return [_walk(v, table, path + (i,)) for i, v in enumerate(node)]
    if isinstance(node, str):
        return _substitute(node, lambda ref: _lookup(ref, table, path), _dotted(path))
    return node


def _lookup(name, table, path):
    if name not in table:
        raise ResolveError(
            f"unknown symbol $({name}); experiment-time symbols come from vars "
            f"or the built-in set, and job-time values use {{{{...}}}} instead",
            _dotted(path),
        )
    return table[name]


def _substitute(value, get, path):
    """Substitute tokens in one value.

    A string that is exactly one token takes that symbol's *type*, so
    ``minvalue: $(obs_land_mask_min)`` becomes the float 0.5 and not the string
    "0.5". A token embedded in surrounding text interpolates as text, and the
    symbol then has to be a scalar.
    """
    if not isinstance(value, str):
        return value

    whole = WHOLE.match(value)
    if whole:
        return get(whole.group(1).strip())

    def replace(match):
        name = match.group(1).strip()
        resolved = get(name)
        if isinstance(resolved, (dict, list)):
            raise ResolveError(
                f"$({name}) is a {type(resolved).__name__} and cannot be "
                f"interpolated into text; reference it on its own to use it whole",
                path,
            )
        if resolved is None:
            raise ResolveError(f"$({name}) is null and cannot be interpolated", path)
        return str(resolved)

    return TOKEN.sub(replace, value)


def unresolved(config):
    """Any ``$(...)`` left after a resolve pass. Should always be empty."""
    found = []

    def walk(node, path):
        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, path + (key,))
        elif isinstance(node, list):
            for i, value in enumerate(node):
                walk(value, path + (i,))
        elif isinstance(node, str) and TOKEN.search(node):
            found.append((_dotted(path), node))

    walk(config, ())
    return found


def _require(config, path):
    cur = config
    for part in path:
        if not isinstance(cur, dict) or part not in cur:
            raise ResolveError(
                "is not set, and the layers interpolate it; set it in your "
                "experiment file", ".".join(path))
        cur = cur[part]
    return cur


def _dotted(path):
    out = ""
    for part in path:
        if isinstance(part, int):
            out += f"[{part}]"
        elif out:
            out += f".{part}"
        else:
            out = str(part)
    return out
