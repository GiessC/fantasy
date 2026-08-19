#!/usr/bin/env bash
# One-shot setup for macOS and Linux:
#
#     ./setup.sh
#
# Finds a suitable Python, builds the virtual environment, installs everything,
# creates your config files, and proves the pipeline works end to end.
# Safe to re-run; it never overwrites a config you have edited.
#
#     ./setup.sh --clean     rebuild the virtual environment from scratch

set -u

CLEAN=0
for arg in "$@"; do
    case "$arg" in
        --clean) CLEAN=1 ;;
        -h|--help) sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "Unknown option: $arg (try --clean)" >&2; exit 2 ;;
    esac
done

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
cd "$ROOT"

# Colour only when attached to a terminal, so redirected output stays clean.
if [ -t 1 ]; then
    CYAN=$'\033[36m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RED=$'\033[31m'; OFF=$'\033[0m'
else
    CYAN=''; GREEN=''; YELLOW=''; RED=''; OFF=''
fi

step() { printf '\n%s==> %s%s\n' "$CYAN" "$1" "$OFF"; }
ok()   { printf '    %s%s%s\n' "$GREEN" "$1" "$OFF"; }
warn() { printf '    %s%s%s\n' "$YELLOW" "$1" "$OFF"; }
fail() {
    printf '\n%sSETUP FAILED%s\n%s%s%s\n\n' "$RED" "$OFF" "$RED" "$1" "$OFF"
    exit 1
}

# ------------------------------------------------------------------ find python

ATTEMPTS=""
FOUND_PYTHON=""

# Sets FOUND_PYTHON and returns 0 when $1 is a real Python 3.11+.
#
# Deliberately NOT called through $(...): the whole point of ATTEMPTS is to
# explain a failure, and a command substitution runs in a subshell, so every
# append to it would be discarded and the report would come out empty.
probe_python() {
    candidate="$1"
    FOUND_PYTHON=""
    if ! command -v "$candidate" >/dev/null 2>&1; then
        ATTEMPTS="$ATTEMPTS
  $candidate -- not installed"
        return 1
    fi
    # No quote characters inside the payload, so quoting stays simple.
    info="$("$candidate" -c 'import sys; print(sys.executable, sys.version_info[0], sys.version_info[1])' 2>/dev/null)"
    if [ -z "$info" ]; then
        ATTEMPTS="$ATTEMPTS
  $candidate -- did not report a version"
        return 1
    fi
    found=""; major=""; minor=""
    read -r found major minor <<EOF
$info
EOF
    if [ "$major" -lt 3 ] || { [ "$major" -eq 3 ] && [ "$minor" -lt 11 ]; }; then
        ATTEMPTS="$ATTEMPTS
  $candidate -- Python $major.$minor, too old (need 3.11+)"
        return 1
    fi
    ATTEMPTS="$ATTEMPTS
  $candidate -- OK, Python $major.$minor at $found"
    FOUND_PYTHON="$found"
    return 0
}

step "Looking for Python 3.11 or newer"

PYTHON=""
# Newest first. python3 is last because on macOS it is usually Apple's 3.9,
# which is too old -- but a Homebrew or python.org install shadows it.
for candidate in python3.14 python3.13 python3.12 python3.11 python3 python; do
    if probe_python "$candidate"; then
        PYTHON="$FOUND_PYTHON"
        break
    fi
done

if [ -z "$PYTHON" ]; then
    fail "No usable Python 3.11 or newer was found.

Every candidate tried, and why each was rejected:
$ATTEMPTS

macOS ships Python 3.9, which is too old. Install a newer one:

    brew install python@3.12

or download it from https://www.python.org/downloads/

Then open a new terminal and run ./setup.sh again."
fi
ok "Using $PYTHON"

# -------------------------------------------------------------------- the venv

VENV="$ROOT/.venv"
VENV_PY="$VENV/bin/python"
FANTASY="$VENV/bin/fantasy-ai"

# A virtual environment is a set of pointers to the interpreter that built it.
# Move it, upgrade that Python, or copy the folder from another machine and
# every command inside it fails. The directory existing proves nothing.
venv_is_usable() {
    [ -x "$VENV_PY" ] || return 1
    home_dir="$(sed -n 's/^ *home *= *//p' "$VENV/pyvenv.cfg" 2>/dev/null | head -1)"
    [ -z "$home_dir" ] || [ -d "$home_dir" ] || return 1
    "$VENV_PY" -c pass >/dev/null 2>&1
}

NEEDS_VENV=1
if [ -d "$VENV" ]; then
    if [ "$CLEAN" -eq 0 ] && venv_is_usable; then
        step "Virtual environment already exists and works"
        ok ".venv"
        NEEDS_VENV=0
    else
        if [ "$CLEAN" -eq 1 ]; then
            step "Rebuilding the virtual environment (--clean)"
        else
            step "The existing .venv is broken -- rebuilding it"
            stale="$(sed -n 's/^ *home *= *//p' "$VENV/pyvenv.cfg" 2>/dev/null | head -1)"
            [ -n "$stale" ] && warn "It points at $stale, which is not usable here."
        fi
        rm -rf "$VENV" || fail "Could not remove $VENV. Delete it by hand and re-run."
        ok "Removed"
    fi
fi

if [ "$NEEDS_VENV" -eq 1 ]; then
    step "Creating the virtual environment (.venv)"
    "$PYTHON" -m venv "$VENV" || fail "Could not create a virtual environment with $PYTHON"
    venv_is_usable || fail "Created .venv but it does not run."
    ok "Created"
fi

step "Installing dependencies (takes a minute the first time)"
"$VENV_PY" -m pip install --upgrade pip --quiet --disable-pip-version-check
"$VENV_PY" -m pip install -e ".[dev]" --quiet --disable-pip-version-check \
    || fail "Installing dependencies failed. The full error is above."
[ -x "$FANTASY" ] || fail "Install reported success but $FANTASY is missing. Try ./setup.sh --clean"
ok "Installed"

# --------------------------------------------------------------------- configs

step "Setting up your config files"
for name in league sources; do
    if [ -f "config/$name.yaml" ]; then
        ok "config/$name.yaml already exists -- left alone"
    else
        cp "config/$name.example.yaml" "config/$name.yaml" \
            || fail "Could not create config/$name.yaml"
        ok "Created config/$name.yaml"
    fi
done

# --------------------------------------------------------------- prove it works

step "Creating the database"
"$FANTASY" db init || fail "Database setup failed. The error is above."

step "Loading demo data (synthetic -- invented players, no network needed)"
"$FANTASY" sync demo || fail "Loading demo data failed. The error is above."

step "Running the analysis to confirm the whole pipeline works"
"$FANTASY" analyze board --limit 10 || fail "Analysis failed. The error is above."

# ----------------------------------------------------------------- what's next

cat <<BANNER

${GREEN}================================================================
 Setup complete. The board above is synthetic demo data.
================================================================${OFF}

Run everything through the wrapper in this folder. You never need to
activate anything:

  ./fantasy-ai analyze board

Next:
  1. Edit config/league.yaml -- teams, scoring, roster, your draft slot.
  2. Put your FantasyPros key in config/sources.yaml, under
     sources.fantasypros.api_key
  3. ./fantasy-ai validate-config      (confirms it read the key)
  4. ./fantasy-ai sync all --verbose   (fetch real data)
  ${YELLOW}5. ./fantasy-ai data clear --source demo${OFF}

${YELLOW}Step 5 matters: the demo players above are INVENTED. They do not
disappear when real data arrives -- they sit on the board next to
real players until you clear them.${OFF}

BANNER
