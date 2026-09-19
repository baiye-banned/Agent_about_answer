#!/usr/bin/env bash
#
# Local secret scan for git-bash / POSIX shells. Run it before committing or
# pushing; CI runs the same checks (plus gitleaks) on every push and PR.
#
# Layer 1 - built-in pattern scan, always on, no external dependency:
#   every file tracked by git plus every untracked file is scanned, including
#   files that .gitignore hides (a key sitting in the working tree is worth
#   flagging even when an ignore rule would keep it out of a normal commit).
#   Only "<file>:<line> [<pattern>]" is reported; the matched text is never
#   printed, so a failing run cannot leak the very secret it found.
#
#   Patterns:
#     - "sk-" followed by 20+ token characters (OpenAI-style keys)
#     - "-----BEGIN ... PRIVATE KEY-----" headers
#     - API_KEY / SECRET / TOKEN / PASSWORD / ... followed by "=" or ":" and a
#       value. Values that are empty, code expressions, pure numbers, or the
#       documented placeholder words are configuration, not credentials, and
#       are not reported.
#
# Layer 2 - gitleaks, used automatically when the binary is on PATH:
#   rule/entropy scan of the working tree and of the full git history.
#
# Exit codes:
#   0  clean
#   1  findings (built-in scan and/or gitleaks)
#   2  bad usage, or gitleaks required but not installed
#
# Requires bash; the file list comes from git when the scanned directory is
# inside a repository and from "find" otherwise.
#
# Usage: bash scripts/scan_secrets.sh [PATH] [--tracked-only] [--require-gitleaks]

set -uo pipefail

REQUIRE_GITLEAKS="${REQUIRE_GITLEAKS:-0}"
TRACKED_ONLY=0
TARGET=""

usage() {
  cat <<'USAGE'
Usage: bash scripts/scan_secrets.sh [PATH] [--tracked-only] [--require-gitleaks]

  PATH                directory to scan (default: the repository containing this script)
  --tracked-only      scan only files already tracked by git (skips untracked and
                      git-ignored files, e.g. a local .env that will never be committed)
  --require-gitleaks  fail (exit 2) when the gitleaks binary is missing; same as
                      REQUIRE_GITLEAKS=1

Exit codes: 0 clean, 1 findings, 2 bad usage or gitleaks required but missing.
USAGE
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --tracked-only) TRACKED_ONLY=1 ;;
    --require-gitleaks) REQUIRE_GITLEAKS=1 ;;
    -h|--help) usage; exit 0 ;;
    -*) echo "scan_secrets: unknown option: $1" >&2; usage >&2; exit 2 ;;
    *) TARGET="$1" ;;
  esac
  shift
done

if [ -n "$TARGET" ]; then
  if [ ! -d "$TARGET" ]; then
    echo "scan_secrets: not a directory: $TARGET" >&2
    exit 2
  fi
  cd "$TARGET" || exit 2
else
  SCRIPT_DIR=$(cd -- "$(dirname -- "$0")" && pwd) || exit 2
  cd "$SCRIPT_DIR/.." || exit 2
fi

GIT_ROOT=$(git rev-parse --show-toplevel 2>/dev/null || true)

RE_SK='sk-[A-Za-z0-9_-]{20,}'
RE_PEM='-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----'
RE_PREFIX='(API_KEY|SECRET|TOKEN|PASSWORD|PASSWD|ACCESS_KEY|PRIVATE_KEY)[A-Za-z0-9_]*[[:space:]]*[:=][[:space:]]*'

TMP_SCAN="${TMPDIR:-/tmp}/scan_secrets.$$"
mkdir -p "$TMP_SCAN" || exit 2
FINDINGS_FILE="$TMP_SCAN/findings.txt"
GITLEAKS_LOG="$TMP_SCAN/gitleaks.log"
: > "$FINDINGS_FILE"
: > "$GITLEAKS_LOG"
trap 'rm -rf "$TMP_SCAN"' EXIT

# Every file to scan: tracked files plus untracked ones. --others also lists
# git-ignored files, because a local .env holding real keys should be visible
# to the scan even though git would never commit it.
collect_files() {
  local entries=() f
  FILES=()
  if [ -n "$GIT_ROOT" ]; then
    if [ "$TRACKED_ONLY" -eq 1 ]; then
      mapfile -d '' -t entries < <(git ls-files -z --cached)
    else
      mapfile -d '' -t entries < <(git ls-files -z --cached --others)
    fi
  else
    mapfile -d '' -t entries < <(find . -type f -print0)
  fi
  for f in ${entries[@]+"${entries[@]}"}; do
    f=${f#./}
    [ -n "$f" ] || continue
    [ -f "$f" ] || continue
    case "$f" in
      .git/*|*/node_modules/*|node_modules/*|*/.venv/*|.venv/*|*/venv/*|venv/*) continue ;;
      */__pycache__/*|__pycache__/*|*/.pytest_cache/*|.pytest_cache/*|*/dist/*|dist/*) continue ;;
    esac
    FILES+=("$f")
  done
}

# A value is a placeholder or a configuration knob - and therefore not a
# finding - when it is empty, a code expression, a pure number (lengths,
# timeouts, counters), or one of the documented stand-in strings.
is_not_secret() {
  local value=$1 lower
  value=${value%\"}; value=${value#\"}
  value=${value%\'}; value=${value#\'}
  value=${value#"${value%%[![:space:]]*}"}
  value=${value%"${value##*[![:space:]]}"}
  lower=${value,,}
  [ -n "$lower" ] || return 0
  case "$lower" in
    *'('*|*')'*|*'['*|*']'*|*'{'*|*'}'*|*'"'*|*"'"*|*'`'*) return 0 ;;
  esac
  if [[ $lower =~ ^[0-9]+$ ]]; then
    return 0
  fi
  case "$lower" in
    none|null|nil|undefined|true|false|yes|no|on|off|password|passwd|secret|token|apikey|api_key|key|todo) return 0 ;;
    change-me*|change_me*|changeme*|change-this*|change_this*|your[-_]*|yourkey*|replace[-_]*|placehold*) return 0 ;;
    example*|sample*|dummy*|fake*|testkey*|not[-_]set*|notset*|unset*|xxxx*|xxx*|'<'*|'$'*) return 0 ;;
    '***'*|'-'|'--'|'...') return 0 ;;
  esac
  return 1
}

# Text that follows the last "KEY = value" / "KEY: value" occurrence on a line.
# Scanning through every match keeps the value correct when a line names the
# variable twice, e.g. SECRET_KEY = os.getenv("SECRET_KEY", "..."): the text
# after the last match is the code expression, which is then not a finding.
line_value() {
  local rest=$1 match pre
  VALUE=""
  while [[ $rest =~ $RE_PREFIX ]]; do
    match=${BASH_REMATCH[0]}
    [ -n "$match" ] || break
    pre=${rest%%"$match"*}
    rest=${rest:$(( ${#pre} + ${#match} ))}
    VALUE=$rest
  done
  VALUE=${VALUE%%[[:space:]]*}
}

scan_builtin() {
  local hits hit content file lineno value
  # -H: always prefix the file name. Without it grep omits the name when a
  # single file is scanned, which would shift the "file:line" parsing below.
  hits=$(grep -HInE -e "$RE_SK" -e "$RE_PEM" -e "$RE_PREFIX" -- "$@" 2>/dev/null || true)
  [ -n "$hits" ] || return 0
  while IFS= read -r hit; do
    [ -n "$hit" ] || continue
    file=${hit%%:*}
    content=${hit#*:}
    lineno=${content%%:*}
    content=${content#*:}
    if [[ $content =~ $RE_SK ]]; then
      printf '%s:%s [sk- token]\n' "$file" "$lineno" >> "$FINDINGS_FILE"
    fi
    if [[ $content =~ $RE_PEM ]]; then
      printf '%s:%s [private key header]\n' "$file" "$lineno" >> "$FINDINGS_FILE"
    fi
    if [[ $content =~ $RE_PREFIX ]]; then
      line_value "$content"
      value=$VALUE
      if ! is_not_secret "$value"; then
        printf '%s:%s [credential assignment]\n' "$file" "$lineno" >> "$FINDINGS_FILE"
      fi
    fi
  done <<< "$hits"
}

FILES=()
collect_files
FILE_TOTAL=${#FILES[@]}
echo "scan_secrets: built-in pattern scan over $FILE_TOTAL file(s) in $PWD"
if [ "$FILE_TOTAL" -gt 0 ]; then
  scan_builtin "${FILES[@]}"
fi

BUILTIN_RC=0
if [ -s "$FINDINGS_FILE" ]; then
  BUILTIN_RC=1
  {
    echo "scan_secrets: FAIL - potential secret(s) in the following location(s):"
    sort -u "$FINDINGS_FILE" | sed 's/^/  /'
    echo "  matched text is intentionally not printed; open the file and rotate/remove the value"
  } >&2
else
  echo "scan_secrets: built-in pattern scan clean"
fi

GITLEAKS_RC=0
if command -v gitleaks >/dev/null 2>&1; then
  echo "scan_secrets: gitleaks found on PATH - running deep scan"
  if [ -n "$GIT_ROOT" ]; then
    gitleaks detect --source "$PWD" --redact --no-banner > "$GITLEAKS_LOG" 2>&1
  else
    gitleaks detect --source "$PWD" --no-git --redact --no-banner > "$GITLEAKS_LOG" 2>&1
  fi
  GITLEAKS_RC=$?
  sed -n '1,80p' "$GITLEAKS_LOG"
  if [ "$GITLEAKS_RC" -ne 0 ]; then
    echo "scan_secrets: FAIL - gitleaks reported findings (secrets are redacted above)" >&2
  else
    echo "scan_secrets: gitleaks scan clean"
  fi
else
  {
    echo "scan_secrets: NOTE - gitleaks is not installed, only the built-in pattern scan ran"
    echo "  full history and entropy based detection need gitleaks:"
    echo "    https://github.com/gitleaks/gitleaks#installing"
    echo "    docker run --rm -v \"\$PWD:/repo\" zricethezav/gitleaks:latest detect --source=/repo --redact -v"
    echo "  CI runs gitleaks on every push and pull request"
  } >&2
  if [ "$REQUIRE_GITLEAKS" = "1" ]; then
    echo "scan_secrets: ERROR - gitleaks is required (--require-gitleaks / REQUIRE_GITLEAKS=1) but not on PATH" >&2
    GITLEAKS_RC=2
  fi
fi

if [ "$BUILTIN_RC" -ne 0 ]; then
  exit 1
fi
if [ "$GITLEAKS_RC" -eq 2 ]; then
  exit 2
fi
if [ "$GITLEAKS_RC" -ne 0 ]; then
  exit 1
fi
exit 0
