# ~/homelab/lib/components.sh: which components are on (COMPONENTS in .env; setup/components/*.toml).
# shellcheck shell=bash
# component_on ID: true if ID is chosen. COMPONENTS unset = an install from before the setup engine,
# where everything runs: true.
component_on() {
  local c
  c=$(sed -n 's/^COMPONENTS=//p' "${ENV_FILE:-.env}" 2>/dev/null | tail -n1 | tr -d "'\"")
  [[ -z $c || ",$c," == *",$1,"* ]]
}
