#!/usr/bin/env bash
#
# Local secret scan for git-bash / POSIX shells (bash 3.2+, i.e. the bash that
# ships with macOS). Run it before committing or pushing; CI runs the same
# checks on every push and pull request.
#
# Layer 1 - built-in pattern scan, always on, no external dependency:
#   every file tracked by git plus every untracked file is scanned, including
#   files that .gitignore hides (a key sitting in the working tree is worth
#   flagging even when an ignore rule would keep it out of a normal commit).
#   Only "<file>:<line> [<pattern>]" is reported; the matched text is never
#   printed, so a failing run cannot leak the very secret it found.
#
#   Patterns (case-sensitive, as written):
#     - "sk-" followed by 20+ token characters (OpenAI-style keys)
#     - "-----BEGIN ... PRIVATE KEY-----" headers
#     - API_KEY / SECRET / TOKEN / PASSWORD / ... followed by "=" or ":" and a
#       value. Values that are empty, code expressions, pure numbers, or the
#       documented placeholder words are configuration, not credentials, and
#       are not reported.
#   Deliberate limit: an assignment whose key name is lowercase (e.g.
#   "password: hunter2" in a config file) is not matched here. Matching those
#   case-insensitively was measured to flag plain code ("token = user.token",
#   "password = self.password") and to cost a process per hit; the gitleaks
#   layer below covers them with entropy-based rules instead.
#
# Layer 2 - gitleaks, required by default:
#   inside a repository: one pass over the commit history and one over the
#   working tree (the working-tree pass is what sees an untracked .env, which
#   a history-only scan cannot see); outside a repository: a working-tree pass.
#   In a git worktree (.git is a file) the history pass cannot run - gitleaks
#   cannot open such a checkout - so it is skipped with a loud note; use a
#   normal clone, or CI, for history scanning.
#
# Exit codes:
#   0  clean
#   1  findings (built-in scan and/or gitleaks)
#   2  bad usage or gitleaks not installed
#
# A scan that quietly skips gitleaks reads as proof that the repository is
# clean when nothing of the sort was checked, so a missing gitleaks is an
# error, not a warning. --patterns-only opts out explicitly.
#
# Usage: bash scripts/scan_secrets.sh [PATH] [--tracked-only] [--patterns-only]
#                                    [--require-gitleaks]

set -uo pipefail

PATTERNS_ONLY=0
TRACKED_ONLY=0
TARGET=""

usage() {
  cat <<'USAGE'
Usage: bash scripts/scan_secrets.sh [PATH] [--tracked-only] [--patterns-only]
                                   [--require-gitleaks]

  PATH                directory to scan (default: the repository containing this script)
  --tracked-only      scan only files already tracked by git (skips untracked and
                      git-ignored files, e.g. a local .env that will never be committed)
  --patterns-only     run the built-in pattern scan only; gitleaks is skipped, so no
                      history and no entropy scan happens (explicit opt-out)
  --require-gitleaks  accepted for compatibility; gitleaks is required by default

Exit codes: 0 clean, 1 findings, 2 bad usage or gitleaks not installed.
USAGE
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --tracked-only) TRACKED_ONLY=1 ;;
    --patterns-only) PATTERNS_ONLY=1 ;;
    --require-gitleaks) : ;;
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

SKIP_DIRS='.git node_modules .venv venv __pycache__ .pytest_cache dist'

keep_file() {
  local f=$1 skip
  f=${f#./}
  [ -n "$f" ] || return 0
  [ -f "$f" ] || return 0
  for skip in $SKIP_DIRS; do
    case "$f" in
      "$skip"/*|*/"$skip"/*) return 0 ;;
    esac
  done
  FILES+=("$f")
}

# Every file to scan: tracked files plus untracked ones. --others also lists
# git-ignored files, because a local .env holding real keys should be visible
# to the scan even though git would never commit it.
# A "while read -d ''" loop is used instead of mapfile: mapfile is bash 4+ and
# silently produces an empty list on bash 3.2, which would turn this scan into
# a clean report over zero files.
collect_files() {
  local f
  FILES=()
  if [ -n "$GIT_ROOT" ]; then
    if [ "$TRACKED_ONLY" -eq 1 ]; then
      while IFS= read -r -d '' f; do keep_file "$f"; done < <(git ls-files -z --cached 2>/dev/null)
    else
      while IFS= read -r -d '' f; do keep_file "$f"; done < <(git ls-files -z --cached --others 2>/dev/null)
    fi
  else
    while IFS= read -r -d '' f; do keep_file "$f"; done < <(find . -type f -print0 2>/dev/null)
  fi
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
  [ -n "$value" ] || return 0
  case "$value" in
    *'('*|*')'*|*'['*|*']'*|*'{'*|*'}'*|*'"'*|*"'"*|*'`'*) return 0 ;;
  esac
  if [[ $value =~ ^[0-9]+$ ]]; then
    return 0
  fi
  # Only values that survive the checks above are lowercased: ${value,,} is
  # bash 4+, and tr (the bash 3.2 replacement) costs one process per call, so
  # it stays off the hot path - most hits are code expressions, already gone.
  lower=$(printf '%s' "$value" | tr '[:upper:]' '[:lower:]')
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
  # -I skips binary files, -H always prefixes the file name (without it grep
  # omits the name when a single file is scanned, which would shift the
  # "file:line" parsing below).
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

# Never report a clean scan over an empty file list while git can see files:
# that is the shape of a silently broken scan, not of a clean repository.
if [ "$FILE_TOTAL" -eq 0 ] && [ -n "$GIT_ROOT" ]; then
  TRACKED_COUNT=$(git ls-files 2>/dev/null | wc -l | tr -d '[:space:]')
  if [ "${TRACKED_COUNT:-0}" -gt 0 ]; then
    echo "scan_secrets: ERROR - git reports $TRACKED_COUNT tracked file(s) but none were collected; refusing to report a clean scan" >&2
    exit 2
  fi
fi

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

# Appends one gitleaks run to the shared log; the exit status is gitleaks'.
gitleaks_pass() {
  local label=$1 src=$2
  shift 2
  echo "scan_secrets: gitleaks - scanning $label"
  gitleaks detect --source "$src" --redact --no-banner "$@" >> "$GITLEAKS_LOG" 2>&1
}

GITLEAKS_RC=0
if [ "$PATTERNS_ONLY" -eq 1 ]; then
  echo "scan_secrets: --patterns-only - gitleaks skipped, so no history and no entropy scan ran"
elif command -v gitleaks >/dev/null 2>&1; then
  if [ -n "$GIT_ROOT" ]; then
    if [ -f "$GIT_ROOT/.git" ]; then
      {
        echo "scan_secrets: NOTE - this checkout is a linked git worktree (.git is a file),"
        echo "  which gitleaks cannot open; the history pass is skipped and only the"
        echo "  working tree is scanned. Run in a normal clone, or rely on CI, for history."
      } >&2
    else
      gitleaks_pass "commit history" "$GIT_ROOT" || GITLEAKS_RC=1
    fi
    gitleaks_pass "working tree" "$PWD" --no-git || GITLEAKS_RC=1
  else
    gitleaks_pass "working tree" "$PWD" --no-git || GITLEAKS_RC=1
  fi
  sed -n '1,120p' "$GITLEAKS_LOG"
  if [ "$GITLEAKS_RC" -ne 0 ]; then
    echo "scan_secrets: FAIL - gitleaks reported findings (secrets are redacted above)" >&2
  else
    echo "scan_secrets: gitleaks scan clean"
  fi
else
  {
    echo "scan_secrets: ERROR - gitleaks is not installed, so the history and entropy"
    echo "  checks did not run; refusing to report a clean scan."
    echo "  install: https://github.com/gitleaks/gitleaks#installing"
    echo "  pattern scan only (no history, no entropy): bash scripts/scan_secrets.sh --patterns-only"
    echo "  CI installs gitleaks and scans the full history on every push and pull request"
  } >&2
  GITLEAKS_RC=2
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
