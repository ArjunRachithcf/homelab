# ~/homelab/lib/components.sh: which components are on (COMPONENTS in .env; setup/components/*.toml).
# shellcheck shell=bash
# component_on ID: true if ID is chosen. COMPONENTS unset = an install from before the setup engine,
# where everything runs: true.
component_on() {
  local c
  c=$(sed -n 's/^COMPONENTS=//p' "${ENV_FILE:-.env}" 2>/dev/null | tail -n1 | tr -d "'\"")
  [[ -z $c || ",$c," == *",$1,"* ]]
}
# component_listed ID: true only if COMPONENTS names ID. For the opt-in components (opt_in in their TOML: none of them
# existed before the setup engine, so an install without COMPONENTS never had them).
component_listed() {
  local c
  c=$(sed -n 's/^COMPONENTS=//p' "${ENV_FILE:-.env}" 2>/dev/null | tail -n1 | tr -d "'\"")
  [[ ",$c," == *",$1,"* ]]
}
