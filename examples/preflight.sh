#!/usr/bin/env bash
# =============================================================================
# preflight.sh — verify a machine can run the petclinic-ci.yml pipeline.
#
# Run this ONCE on the self-hosted GitHub Actions runner before opening the
# first test PR. It checks everything the workflow assumes but cannot install
# for you: Python venv, the CLI, JDK, Maven, and the .env API key.
#
#   bash examples/preflight.sh
#
# Exit code: 0 if every hard check passed, 1 otherwise. Soft checks (marked
# WARN) never fail the script — they just flag things you probably want set.
# =============================================================================

set -u

# Resolve the workspace root (this script lives in <root>/examples/).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV_PY="$ROOT/venv/bin/python"

FAIL=0
pass() { printf '  \033[32mPASS\033[0m  %s\n' "$1"; }
fail() { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; FAIL=1; }
warn() { printf '  \033[33mWARN\033[0m  %s\n' "$1"; }

echo "preflight — ai-engineering-workspace @ $ROOT"
echo

# --- 1. Python venv ----------------------------------------------------------
echo "[1] Python environment"
if [ -x "$VENV_PY" ]; then
  PYV="$("$VENV_PY" -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null)"
  case "$PYV" in
    3.1[2-9]|3.[2-9]*) pass "venv Python $PYV at venv/bin/python" ;;
    *) fail "venv Python is $PYV — need 3.12+" ;;
  esac
else
  fail "no venv at venv/bin/python — create it: python3 -m venv venv && venv/bin/pip install -r requirements.txt"
fi

# --- 2. CLI responds ---------------------------------------------------------
echo "[2] CLI subcommands (the ones petclinic-ci.yml invokes)"
if [ -x "$VENV_PY" ]; then
  ( cd "$ROOT" && "$VENV_PY" -m cli.main --help >/dev/null 2>&1 ) \
    && pass "python -m cli.main responds" \
    || fail "python -m cli.main failed — check requirements are installed"
  for sub in "scan" "review" "verify health-check" "verify generate" "verify run" "apply"; do
    # shellcheck disable=SC2086
    if ( cd "$ROOT" && "$VENV_PY" -m cli.main $sub --help >/dev/null 2>&1 ); then
      pass "subcommand '$sub'"
    else
      fail "subcommand '$sub' did not respond"
    fi
  done
else
  fail "skipped — no venv"
fi

# --- 3. JDK ------------------------------------------------------------------
echo "[3] JDK (petclinic build)"
if command -v java >/dev/null 2>&1; then
  JV="$(java -version 2>&1 | head -1)"
  # Major version: "17.0.1" -> 17, "1.8.0" -> 8, "21" -> 21
  MAJOR="$(echo "$JV" | sed -E 's/.*version "([0-9]+)\.?([0-9]*).*/\1 \2/' | awk '{print ($1==1)?$2:$1}')"
  if [ -n "$MAJOR" ] && [ "$MAJOR" -ge 17 ] 2>/dev/null; then
    pass "Java $MAJOR ($JV)"
  else
    fail "Java major version is '$MAJOR' — need 17+  ($JV)"
  fi
else
  fail "java not on PATH"
fi

# --- 4. Maven ----------------------------------------------------------------
echo "[4] Maven (petclinic build)"
if command -v mvn >/dev/null 2>&1; then
  pass "$(mvn -version 2>/dev/null | head -1)"
else
  fail "mvn not on PATH"
fi

# --- 5. .env -----------------------------------------------------------------
echo "[5] .env configuration"
ENV_FILE="$ROOT/.env"
if [ -f "$ENV_FILE" ]; then
  KEY="$(grep -E '^ANTHROPIC_API_KEY=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '"'"'"' ')"
  case "$KEY" in
    ""|your_anthropic_api_key) fail "ANTHROPIC_API_KEY missing or still the placeholder — run: python -m cli.main init" ;;
    sk-ant-*) pass "ANTHROPIC_API_KEY is set" ;;
    *) warn "ANTHROPIC_API_KEY is set but doesn't start with sk-ant- — double-check it" ;;
  esac
  grep -qE '^GITHUB_REPO=' "$ENV_FILE"          && pass "GITHUB_REPO is set"          || warn "GITHUB_REPO not in .env (CI injects it; only needed for local runs)"
  grep -qE '^REVIEW_ALLOWED_REPOS=' "$ENV_FILE" && pass "REVIEW_ALLOWED_REPOS is set" || warn "REVIEW_ALLOWED_REPOS not in .env (CI injects it from the repo secret)"
else
  fail "no .env at $ENV_FILE — run: python -m cli.main init"
fi

# --- 6. git ------------------------------------------------------------------
echo "[6] git (apply needs it to push fixes)"
command -v git >/dev/null 2>&1 && pass "$(git --version)" || fail "git not on PATH"

# --- summary -----------------------------------------------------------------
echo
if [ "$FAIL" -eq 0 ]; then
  printf '\033[32mpreflight passed.\033[0m The runner can execute petclinic-ci.yml.\n'
  echo "Still verify on GitHub (not checkable here):"
  echo "  - the self-hosted runner shows 'Idle' under Settings > Actions > Runners"
  echo "  - secrets ANTHROPIC_API_KEY + REVIEW_ALLOWED_REPOS and variable AI_TOOL_PATH are set"
  exit 0
else
  printf '\033[31mpreflight failed.\033[0m Fix the FAIL items above before opening a PR.\n'
  exit 1
fi
