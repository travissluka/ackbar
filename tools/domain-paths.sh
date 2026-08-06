# Where a domain's model configuration lives. Sourced, not run.
#
#   source "$ACKBAR_ROOT/tools/domain-paths.sh"
#   domain_paths gom_25km      # sets BASE, DATA and OVERRIDE
#
# The three paths are read out of the domain's own layer rather than derived
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
declared = yaml.safe_load(open(layer)).get("vars") or {}
builtin = {"ackbar_root": root, "static_root": static}

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
                   ("OVERRIDE", "mom6_override_dir")):
    print(f"{shell}={resolve(var)}")
EOF
    ) || return 1

    # eval rather than read, so a missing variable stays a failure above rather
    # than becoming an empty path that some later mkdir -p invents.
    eval "$resolved"
}
