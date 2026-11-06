# ~/homelab/lib/wizard.sh: the wizard library, sourced by every *-wizard.sh (plan Phase 2b).
# shellcheck shell=bash
# shellcheck source=lib/need-bash.sh
source "$(dirname "${BASH_SOURCE[0]}")/need-bash.sh"   # bash 4.4+ (macOS: Homebrew's)
#
# Two front ends, the same stages:
#   HOMELAB_UI=tty       (default) the terminal: colours, cleared screens, readline prompts.
#   HOMELAB_UI=protocol  one message per line on stdout, each starting with the byte 0x1E and a
#                        JSON object; every other stdout line is the commands' own output. Answers
#                        come back on stdin, one line each. lib/setup/run.py drives this for
#                        `./homelab setup --protocol` (the setup app) and the MCP server.
#                        Text marks emphasis with ⟦b⟧ ⟦d⟧ ⟦r⟧ ⟦y⟧ ⟦g⟧ ⟦u⟧ (bold, dim, red, yellow, green,
#                        blue) and ⟦/⟧ (end); a front end renders or strips them.
# Messages ("t"): banner, stage, say (style say|step|note|warn), link, pause, confirm, input,
# secret, lines, danger, terminal, wrote, check, finish. Secret answers go into the wizard and are
# never repeated in a message (only "wrote KEY").
#
# Everything a wizard needs: stage, say, step, note, warn, open_url, pause, confirm, ask,
# ask_secret, ask_lines, danger, in_terminal, write_env, _existing, check, retry_check,
# skip_offer, finish; FAILED, SKIPPED, WRITTEN_ENV; colours BOLD DIM RESET BLUE GREEN YELLOW RED.

HOMELAB_UI=${HOMELAB_UI:-tty}

# ──────────────────────────────────────────────────────────────────────────
# Wizard library: delightful, consistent UX, identical across every wizard.
# ──────────────────────────────────────────────────────────────────────────

if [[ $HOMELAB_UI == protocol ]]; then
  BOLD="⟦b⟧"; DIM="⟦d⟧"; RESET="⟦/⟧"; BLUE="⟦u⟧"; GREEN="⟦g⟧"; YELLOW="⟦y⟧"; RED="⟦r⟧"
elif [[ -t 1 ]] && command -v tput >/dev/null 2>&1 && [[ "$(tput colors 2>/dev/null || echo 0)" -ge 8 ]]; then
  BOLD=$(tput bold); DIM=$(tput dim); RESET=$(tput sgr0)
  BLUE=$(tput setaf 4); GREEN=$(tput setaf 2); YELLOW=$(tput setaf 3); RED=$(tput setaf 1)
else
  BOLD=""; DIM=""; RESET=""; BLUE=""; GREEN=""; YELLOW=""; RED=""
fi

# Author sets this at the top of the stages section.
TOTAL_STAGES=0

_STAGE_INDEX=0
ENV_FILE="${ENV_FILE:-.env}"
WRITTEN_ENV=()    # KEYs written to ENV_FILE this run
WRITTEN_SECRET=() # secret NAMEs set this run
SKIPPED=()        # things we couldn't do (e.g. gh missing)
FAILED=()         # checks that failed

# ── Protocol mode ──────────────────────────────────────────────────────────
_proto() { [[ $HOMELAB_UI == protocol ]]; }
# _js "text": a JSON string (control characters dropped)
_js() {
  local s
  s=$(printf '%s' "$1" | LC_ALL=C tr -d '\000-\010\013\014\016-\037')
  s=${s//\\/\\\\}; s=${s//\"/\\\"}; s=${s//$'\n'/\\n}; s=${s//$'\t'/\\t}; s=${s//$'\r'/}
  printf '"%s"' "$s"
}
# _emit '"t":"say",...' (the inside of the JSON object)
_emit() { printf '\036{%s}\n' "$1"; }
# _answer VAR: one line from the front end
_answer() { local _a; IFS= read -r _a || return 1; printf -v "$1" '%s' "$_a"; }
# The current stage's id: its function name (stage_disk → disk)
_stage_id() { local f; for f in "${FUNCNAME[@]}"; do [[ $f == stage_* ]] && { printf '%s' "${f#stage_}"; return; }; done; }

# _clear wipes the terminal so only the current step is on screen. No-op when
# output isn't a terminal, so piped logs stay readable.
_clear() {
  [[ -t 1 ]] || return 0
  if command -v tput >/dev/null 2>&1 && tput clear 2>/dev/null; then return 0; fi
  printf '\033[2J\033[3J\033[H'
}

# banner "Title" shows the opening frame: what this wizard does.
banner() {
  if _proto; then _emit "\"t\":\"banner\",\"title\":$(_js "$1"),\"stages\":${HOMELAB_STAGES_TOTAL:-$TOTAL_STAGES}"; pause "Ready to start?"; return; fi
  _clear
  printf '\n%s%s  %s%s\n' "$BOLD" "$BLUE" "$1" "$RESET"
  printf '%s  %s stages%s\n\n' "$DIM" "${HOMELAB_STAGES_TOTAL:-$TOTAL_STAGES}" "$RESET"
  printf '%s  You drive the browser; this wizard tells you exactly what to do and\n' "$DIM"
  printf '  captures the values you copy back. Stop any time with Ctrl-C and re-run\n'
  printf '  later, since it remembers values already saved.%s\n' "$RESET"
  pause "Ready to start?"
}

# stage "Name" clears the screen, then announces a stage and shows progress.
# Clearing keeps only the current step on screen.
# HOMELAB_STAGES (comma-separated ids, set by ./homelab setup from the chosen components): a stage not
# in it returns 1, and the stage function returns there (each starts with `stage "…" || return 0`).
stage() {
  local id total=${HOMELAB_STAGES_TOTAL:-$TOTAL_STAGES}
  id=$(_stage_id)
  if [[ -n ${HOMELAB_STAGES:-} && ",$HOMELAB_STAGES," != *",$id,"* ]]; then return 1; fi
  _STAGE_INDEX=$((_STAGE_INDEX + 1))
  if _proto; then
    _emit "\"t\":\"stage\",\"id\":$(_js "$id"),\"n\":$_STAGE_INDEX,\"of\":$total,\"title\":$(_js "$1")"
    return
  fi
  _clear
  printf '\n%s%s▸ Stage %s/%s · %s%s\n' \
    "$BOLD" "$BLUE" "$_STAGE_INDEX" "$total" "$1" "$RESET"
}

# say "..." prints a plain instruction line.
say()  { if _proto; then _emit "\"t\":\"say\",\"style\":\"say\",\"text\":$(_js "$1")"; else printf '  %s\n' "$1"; fi; }
# step "..." is a numbered-feeling action the human takes in the browser.
step() { if _proto; then _emit "\"t\":\"say\",\"style\":\"step\",\"text\":$(_js "$1")"; else printf '  %s•%s %s\n' "$BLUE" "$RESET" "$1"; fi; }
note() { if _proto; then _emit "\"t\":\"say\",\"style\":\"note\",\"text\":$(_js "$1")"; else printf '  %s%s%s\n' "$DIM" "$1" "$RESET"; fi; }
warn() { if _proto; then _emit "\"t\":\"say\",\"style\":\"warn\",\"text\":$(_js "$1")"; else printf '  %s⚠ %s%s\n' "$YELLOW" "$1" "$RESET"; fi; }

# open_url URL opens it in the human's browser, cross-platform incl. WSL.
# (protocol: the front end shows the link; nothing opens on the server)
open_url() {
  local url="$1"
  if _proto; then _emit "\"t\":\"link\",\"url\":$(_js "$url")"; return; fi
  printf '  %s↗ opening%s %s\n' "$GREEN" "$RESET" "$url"
  { if   command -v wslview     >/dev/null 2>&1; then wslview "$url"
    elif command -v explorer.exe >/dev/null 2>&1; then explorer.exe "$url"
    elif command -v xdg-open    >/dev/null 2>&1; then xdg-open "$url"
    elif command -v open        >/dev/null 2>&1; then open "$url"
    else false; fi
  } >/dev/null 2>&1 || warn "couldn't open a browser, so visit it manually: $url"
}

# pause "msg" waits for the human to confirm they've done the manual part.
pause() {
  if _proto; then _emit "\"t\":\"pause\",\"text\":$(_js "${1:-Press Enter to continue}")"; _answer _ || true; return; fi
  printf '  %s%s%s ' "$DIM" "${1:-Press Enter to continue}" "$RESET"
  read -r _ || true
}

# confirm "question" is a y/N gate; returns success on yes.
confirm() {
  local reply=""
  if _proto; then
    _emit "\"t\":\"confirm\",\"text\":$(_js "$1")"; _answer reply || true
  else
    printf '  %s? %s [y/N] ' "$YELLOW" "$1"
    read -r reply || true
  fi
  [[ "$reply" =~ ^[Yy] ]]
}

# _existing KEY: current value of KEY in ENV_FILE, if any, with write_env's single
# quotes or a plain dotenv double-quoted value undone.
_existing() {
  [[ -f "$ENV_FILE" ]] || return 1
  local line value sq="'\\''"; line=$(grep -E "^${1}=" "$ENV_FILE" | tail -n1) || return 1
  value="${line#*=}"
  if [[ "$value" == \'*\' ]]; then value="${value:1:${#value}-2}"; value="${value//"$sq"/\'}"
  elif [[ "$value" == \"*\" ]]; then
    value="${value:1:${#value}-2}"
    [[ "$value" == *[\\\$\"]* ]] && return 1   # escapes, $ or ": can't decode safely; ask again
  fi
  printf '%s' "$value"
}

# ask KEY "Prompt" reads a value into $KEY. Offers the existing .env value as
# a default on re-runs (Enter keeps it). Visible input (non-secret), edited
# with Readline. Fails at EOF with no input.
ask() {
  local key="$1" prompt="$2" current input s=$'\001' e=$'\002' p rc=0
  current=$(_existing "$key" || true)
  if _proto; then
    _emit "\"t\":\"input\",\"key\":$(_js "$key"),\"prompt\":$(_js "$prompt"),\"current\":$(_js "$current")"
    _answer input || rc=1
  else
    # Readline needs the colour codes wrapped in \001..\002 to place the cursor.
    p="  $s$BOLD$e$prompt$s$RESET$e "
    [[ -n "$current" ]] && p+="$s$DIM${e}[Enter keeps current]$s$RESET$e "
    read -e -r -p "$p" input || rc=1
  fi
  if (( rc )) && [[ -z "$input" ]]; then return 1; fi
  [[ -z "$input" && -n "$current" ]] && input="$current"
  printf -v "$key" '%s' "$input"
}

# ask_secret KEY "Prompt" is like ask, but input is hidden.
ask_secret() {
  local key="$1" prompt="$2" current input rc=0
  current=$(_existing "$key" || true)
  if _proto; then
    _emit "\"t\":\"secret\",\"key\":$(_js "$key"),\"prompt\":$(_js "$prompt"),\"current_set\":$([[ -n "$current" ]] && echo true || echo false)"
    _answer input || rc=1
  else
    if [[ -n "$current" ]]; then
      printf '  %s%s%s %s[Enter keeps current]%s ' "$BOLD" "$prompt" "$RESET" "$DIM" "$RESET"
    else
      printf '  %s%s%s ' "$BOLD" "$prompt" "$RESET"
    fi
    read -rs input || rc=1
    printf '\n'
  fi
  if (( rc )) && [[ -z "$input" ]]; then return 1; fi
  [[ -z "$input" && -n "$current" ]] && input="$current"
  printf -v "$key" '%s' "$input"
}

# ask_lines VAR "Prompt": several lines (e.g. public keys) until an empty line, into $VAR
ask_lines() {
  local var="$1" prompt="$2" line all=""
  if _proto; then _emit "\"t\":\"lines\",\"prompt\":$(_js "$prompt")"; else say "$prompt"; fi
  while true; do
    if _proto; then _answer line || break; else read -r -p "  > " line || break; fi
    [[ -z "$line" ]] && break
    all+="$line"$'\n'
  done
  printf -v "$var" '%s' "$all"
}

# danger "What happens" WORD: the human must type WORD to go on (a disk wipe, …). Never answered
# by an AI front end (the MCP server refuses these).
danger() {
  local text="$1" word="$2" reply=""
  if _proto; then
    _emit "\"t\":\"danger\",\"text\":$(_js "$text"),\"word\":$(_js "$word")"; _answer reply || true
  else
    warn "$text"
    printf '  %s? Type %s%s%s to go on, anything else stops: ' "$YELLOW" "$BOLD" "$word" "$RESET"
    read -r reply || true
  fi
  [[ "$reply" == "$word" ]]
}

# in_terminal "why" CMD…: a command that needs a real terminal (a password prompt of its own).
# tty: runs it here. protocol: asks the human to run it in a terminal on this machine, then goes on.
in_terminal() {
  local why="$1"; shift
  if _proto; then
    _emit "\"t\":\"terminal\",\"text\":$(_js "$why"),\"command\":$(_js "cd $PWD && $*")"
    _answer _ || true
    return 0
  fi
  "$@"
}

# write_env KEY VALUE upserts KEY='VALUE' into ENV_FILE (replaces any existing
# line) and sets $KEY. Idempotent. Creates ENV_FILE at mode 0600; writes through
# a symlink and keeps an existing file's mode.
write_env() {
  local key="$1" value="$2" tmp sq="'\\''"
  [[ -e "$ENV_FILE" ]] || (umask 077 && : > "$ENV_FILE")
  tmp=$(mktemp)
  grep -vE "^${key}=" "$ENV_FILE" > "$tmp" || true
  printf "%s='%s'\n" "$key" "${value//\'/$sq}" >> "$tmp"
  cat "$tmp" > "$ENV_FILE"
  rm -f "$tmp"
  printf -v "$key" '%s' "$value"
  WRITTEN_ENV+=("$key")
  if _proto; then _emit "\"t\":\"wrote\",\"key\":$(_js "$key")"; return; fi
  printf '  %s✓ wrote%s %s → %s\n' "$GREEN" "$RESET" "$key" "$ENV_FILE"
}

# set_secret NAME VALUE sets a GitHub Actions repo secret via gh. Falls back
# to a warning (and records it) if gh is unavailable or unauthenticated.
set_secret() {
  local name="$1" value="$2"
  if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
    if printf '%s' "$value" | gh secret set "$name" >/dev/null 2>&1; then
      WRITTEN_SECRET+=("$name")
      printf '  %s✓ set%s GitHub secret %s\n' "$GREEN" "$RESET" "$name"
      return
    fi
  fi
  SKIPPED+=("GitHub secret $name (set it manually: gh secret set $name)")
  warn "skipped GitHub secret $name: gh not ready; set it later"
}

# set_var NAME VALUE sets a GitHub Actions repo variable (non-secret).
set_var() {
  local name="$1" value="$2"
  if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
    if gh variable set "$name" --body "$value" >/dev/null 2>&1; then
      printf '  %s✓ set%s GitHub variable %s\n' "$GREEN" "$RESET" "$name"
      return
    fi
  fi
  SKIPPED+=("GitHub variable $name")
  warn "skipped GitHub variable $name, gh not ready; set it later"
}

# check "label" cmd...  runs cmd quietly and prints ✓/✗; failures are listed at the end.
check() {
  local label="$1"; shift
  if "$@" >/dev/null 2>&1; then
    if _proto; then _emit "\"t\":\"check\",\"label\":$(_js "$label"),\"ok\":true"; else
    printf '  %s✓%s %s\n' "$GREEN" "$RESET" "$label"; fi
  else
    if _proto; then _emit "\"t\":\"check\",\"label\":$(_js "$label"),\"ok\":false"; else
    printf '  %s✗%s %s\n' "$RED" "$RESET" "$label"; fi
    FAILED+=("$label"); return 1
  fi
}

# retry_check "label" cmd...  like check, but a failure offers retry (after a fix) or skip.
retry_check() {
  while ! check "$@"; do
    unset 'FAILED[${#FAILED[@]}-1]'
    if confirm "Fix it, then retry the check (no = skip and note it)"; then continue; fi
    FAILED+=("$1"); return 1
  done
}

skip_offer() { confirm "Already did this stage on an earlier run? Skip it"; }

# distro_family: fedora | debian | arch | macos | other (Debian also for Ubuntu), from /etc/os-release
distro_family() {
  [[ "$(uname -s)" == Darwin ]] && { echo macos; return; }
  local ids
  ids=" $(. /etc/os-release 2>/dev/null; echo "${ID:-} ${ID_LIKE:-}") "
  case "$ids" in
    *" fedora "*|*" rhel "*) echo fedora ;;
    *" debian "*|*" ubuntu "*) echo debian ;;
    *" arch "*) echo arch ;;
    *) echo other ;;
  esac
}

# install_pkgs NAME… : packages by their Fedora names, with this distro's manager and names
# (ansible-core and its collections are one package, ansible, elsewhere)
install_pkgs() {
  local fam names=() n
  fam=$(distro_family)
  for n in "$@"; do
    case "$fam:$n" in
      fedora:*) names+=("$n") ;;
      *:ansible-collection-*) ;;
      *:ansible-core) names+=(ansible) ;;
      *) names+=("$n") ;;
    esac
  done
  case "$fam" in
    fedora) sudo dnf -y -q install "${names[@]}" ;;
    debian) sudo apt-get install -y -q "${names[@]}" ;;
    arch) sudo pacman -S --noconfirm --needed "${names[@]}" ;;
    macos) brew install "${names[@]}" ;;
    *) warn "Install these yourself: ${names[*]}"; return 1 ;;
  esac
}

# finish clears, then shows a closing summary of everything configured.
finish() {
  if _proto; then
    local k list=""
    for k in "${WRITTEN_ENV[@]}"; do list+="${list:+,}$(_js "$k")"; done
    local sk=""; for k in "${SKIPPED[@]}"; do sk+="${sk:+,}$(_js "$k")"; done
    _emit "\"t\":\"finish\",\"wrote\":[$list],\"todo\":[$sk]"
    return
  fi
  _clear
  printf '\n%s%s  ✓ Setup complete%s\n' "$BOLD" "$GREEN" "$RESET"
  (( ${#WRITTEN_ENV[@]} ))    && note "wrote ${#WRITTEN_ENV[@]} value(s) to $ENV_FILE: ${WRITTEN_ENV[*]}"
  (( ${#WRITTEN_SECRET[@]} )) && note "set ${#WRITTEN_SECRET[@]} GitHub secret(s): ${WRITTEN_SECRET[*]}"
  if (( ${#SKIPPED[@]} )); then
    printf '\n'; warn "still to do by hand:"
    for s in "${SKIPPED[@]}"; do note "  - $s"; done
  fi
  printf '\n'
}

# The homelab checks the wizards share (NetBird zones, the public path, Authentik apps, secrets)
# shellcheck source=lib/wizard-checks.sh
source "$(dirname "${BASH_SOURCE[0]}")/wizard-checks.sh"
