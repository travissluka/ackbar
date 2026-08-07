# Where a domain's model configuration lives. Sourced, not run.
#
#   source "$ACKBAR_ROOT/tools/domain-paths.sh"
#   domain_paths gom_25km      # sets BASE, DATA, OVERRIDE, STATIC and LEVELS
#
# The values are read out of the domain's own layer rather than derived
# from its name, because no rule over the name is right for all of them: the
# Gulf resolutions share one base directory between four domains, and om_1deg's
# is upstream's inside the MOM6-examples submodule. The layer is where a domain
# says what it is made of, and an offline stage that guessed instead would be a
# second answer to that question, free to disagree with the one the workflow
# uses.
#
# Needs $ACKBAR_ROOT, and $ACKBAR_STATIC_ROOT for any domain whose data is
# staged (which is all of them but om_1deg).

domain_paths() {
    local domain=${1:?domain_paths <domain>}
    local layer=$ACKBAR_ROOT/config/layers/domain/$domain.yaml
    [[ -e $layer ]] || {
        echo "domain-paths: no layer at $layer, so $domain is not a domain" >&2
        return 1
    }

    local resolved
    resolved=$(python3 - "$layer" "$ACKBAR_ROOT" "${ACKBAR_STATIC_ROOT:-}" <<'EOF'
import re, sys, yaml

layer, root, static = sys.argv[1:4]
builtin = {"ackbar_root": root, "static_root": static}


def vars_of(path, seen=()):
    """A layer's vars with everything it inherits merged underneath it.

    The same rule `ackbar.config.merge_layers` applies and for the same reason:
    a domain layer states its slug and its resolution and leaves the paths built
    from those to `domain/common/<family>.yaml`. Reading the layer alone finds
    a `vars` block with two entries in it and none of the four this needs.
    """
    if path in seen:
        sys.exit(f"domain-paths: {path} inherits itself")
    document = yaml.safe_load(open(path)) or {}
    merged = {}
    for parent in document.get("inherit") or ():
        merged.update(vars_of(f"{root}/config/layers/{parent}.yaml", seen + (path,)))
    merged.update(document.get("vars") or {})
    return merged


declared = vars_of(layer)


def resolve(name, seen=()):
    if name in seen:
        sys.exit(f"domain-paths: {name} refers to itself")
    if name in builtin:
        return builtin[name]
    if name not in declared:
        sys.exit(f"domain-paths: {layer} does not declare {name}")
    return re.sub(r"\$\((\w+)\)",
                  lambda m: resolve(m.group(1), seen + (name,)),
                  str(declared[name]))

for shell, var in (("BASE", "mom6_base_dir"),
                   ("DATA", "mom6_input_dir"),
                   ("OVERRIDE", "mom6_override_dir"),
                   # Where SOCA's view of the domain is kept: the gridspec and
                   # the diffusion calibration. The same var the domain layer
                   # points `domain.static` at, so an offline stage writes
                   # exactly where the workflow will look.
                   ("STATIC", "domain_static"),
                   # The domain's NK, which every configuration that reads a
                   # three dimensional field off disk has to restate.
                   ("LEVELS", "diffusion_levels")):
    print(f"{shell}={resolve(var)}")
EOF
    ) || return 1

    # eval rather than read, so a missing variable stays a failure above rather
    # than becoming an empty path that some later mkdir -p invents.
    eval "$resolved"
}
