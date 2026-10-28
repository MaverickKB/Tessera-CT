#!/usr/bin/env bash
# Pre-commit review: fail if tracked, staged, untracked, or exported source
# embeds private hosts, IPs, user paths, or credentials. Loopback and
# example.* placeholders are fine.
#
# Personal / deployment-specific patterns belong in an uncommitted local file
# (default .tessera-hardwire-patterns.local, one regex per line, # comments ok)
# or in TESSERA_HARDWIRE_PATTERNS (newline- or |-separated). The committed
# script ships only generic checks.
#
# Personal computer hostnames (*MacBook*) and Apple Developer Team IDs are
# generic checks. Known private host names are one name per line in the
# gitignored file .tessera-private-hosts.local (override with
# TESSERA_PRIVATE_HOSTS_FILE). A listed name matches as a whole token, so a
# longer name that only starts with it does not match.
#
# Inside a work tree, git ls-files names tracked paths and the untracked paths
# that survive the standard excludes. git grep reads both the work tree and the
# index, so cleaning the checkout cannot hide a staged value.
#
# A source export or tarball has no work tree. find walks that tree and rg
# applies the same excludes: this script, bytecode, and the export's own
# gitignore. Ignored artifacts (.venv, *.pyc) are not release inputs. The
# operator's global ignore file is not applied: export files correspond to
# tracked sources, which exclude-standard does not hide. Every rg call passes
# --no-config so RIPGREP_CONFIG_PATH or a ripgreprc cannot add globs that drop
# files and let a hardwire through. find has no exclusion config. git grep -E
# overrides grep.patternType, and an alias cannot replace that builtin.
# core.excludesFile only feeds the existing --exclude-standard untracked
# filter; tracked paths are still listed.
#
# The whole script stays within bash 3.2. macOS /bin/bash is 3.2.57, and the
# pre-commit hook runs this file with `bash`. There is no associative array,
# mapfile, or other bash 4 feature. The export file sets meet in `comm`.
# If the export walk cannot run, the check fails closed instead of reporting
# a clean tree.
set -euo pipefail

# An inherited GIT_DIR can name some other checkout. Only trust git when the
# process's own directory sits inside that work tree; otherwise this is an
# export and must be walked in place.
IN_WORK_TREE=0
if root=$(git rev-parse --show-toplevel 2>/dev/null); then
  here=$(pwd -P)
  root_phys=$(cd "$root" && pwd -P)
  if [ "$here" = "$root_phys" ] || [ "${here#"$root_phys"/}" != "$here" ]; then
    cd "$root"
    IN_WORK_TREE=1
  fi
fi

GENERIC='(100\.[0-9]+\.[0-9]+\.[0-9]+|[0-9]{1,3}(\.[0-9]{1,3}){3}:[0-9]{2,5}|/Users/[A-Za-z][^/"'\'']*|/home/[a-z]+/|/volume[0-9]+/|sk-[A-Za-z0-9]{20,}|Bearer [A-Za-z0-9._-]{20,})'
PRIVATE_IPS='(10\.[0-9]+\.[0-9]+\.[0-9]+|172\.(1[6-9]|2[0-9]|3[01])\.[0-9]+\.[0-9]+|192\.168\.[0-9]+\.[0-9]+|169\.254\.[0-9]+\.[0-9]+)'
PRIVATE_IPV6='([fF][cCdD][0-9A-Fa-f]{2}|[fF][eE][89aAbB][0-9A-Fa-f])(:[0-9A-Fa-f]*){2,}'
PRIVATE_HOSTS='https?://[A-Za-z0-9._-]+\.(internal|lan|local|home|corp|ts\.net)([^A-Za-z0-9_.-]|$)'
TOKENS='(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})'
LOCAL_FILE="${TESSERA_HARDWIRE_PATTERNS_FILE:-.tessera-hardwire-patterns.local}"
HOSTS_FILE="${TESSERA_PRIVATE_HOSTS_FILE:-.tessera-private-hosts.local}"
EXTRA=""
if [ -f "$LOCAL_FILE" ]; then
  EXTRA=$(grep -vE '^\s*(#|$)' "$LOCAL_FILE" | paste -sd'|' - || true)
fi
if [ -n "${TESSERA_HARDWIRE_PATTERNS:-}" ]; then
  ENV_PAT=$(printf '%s' "$TESSERA_HARDWIRE_PATTERNS" | tr '|' '\n' | grep -vE '^\s*$' | paste -sd'|' - || true)
  if [ -n "$ENV_PAT" ]; then
    EXTRA="${EXTRA:+$EXTRA|}$ENV_PAT"
  fi
fi
# A team id is 10 letters and digits and includes a letter, so a 10-digit
# build mask is not one. The plist form keeps the id on its own string line.
MACBOOK_HOSTS='[A-Za-z0-9][A-Za-z0-9._-]*MacBook[A-Za-z0-9._-]*'
APPLE_TEAM_BODY='([A-Z][A-Z0-9]{9}|[A-Z0-9][A-Z][A-Z0-9]{8}|[A-Z0-9]{2}[A-Z][A-Z0-9]{7}|[A-Z0-9]{3}[A-Z][A-Z0-9]{6}|[A-Z0-9]{4}[A-Z][A-Z0-9]{5}|[A-Z0-9]{5}[A-Z][A-Z0-9]{4}|[A-Z0-9]{6}[A-Z][A-Z0-9]{3}|[A-Z0-9]{7}[A-Z][A-Z0-9]{2}|[A-Z0-9]{8}[A-Z][A-Z0-9]|[A-Z0-9]{9}[A-Z])'
APPLE_TEAM="(DEVELOPMENT_TEAM[[:space:]]*=[[:space:]]*\"?${APPLE_TEAM_BODY}\"?|developmentTeam[[:space:]]*=[[:space:]]*\"${APPLE_TEAM_BODY}\"|<string>${APPLE_TEAM_BODY}</string>)"
if [ -f "$HOSTS_FILE" ]; then
  while IFS= read -r host || [ -n "$host" ]; do
    host=${host%$'\r'}
    host=${host#"${host%%[![:space:]]*}"}
    host=${host%"${host##*[![:space:]]}"}
    case "$host" in
      ''|\#*) continue ;;
    esac
    case "$host" in
      *[!A-Za-z0-9._-]*)
        echo "Hardwire check could not read the private host list." >&2
        exit 2
        ;;
    esac
    escaped=$(printf '%s' "$host" | sed 's/[][\\.^$*+?{}|()]/\\&/g')
    piece="(^|[^A-Za-z0-9_-])${escaped}([^A-Za-z0-9_-]|$)"
    EXTRA="${EXTRA:+$EXTRA|}$piece"
  done < "$HOSTS_FILE"
fi
PATTERN="$GENERIC|$PRIVATE_IPS|$PRIVATE_IPV6|$PRIVATE_HOSTS|$TOKENS|$MACBOOK_HOSTS|$APPLE_TEAM"
if [ -n "$EXTRA" ]; then
  PATTERN="${PATTERN}|${EXTRA}"
fi

# Filter individual matches, never an entire source line. An example URL beside a
# private value must not hide that value from the review.
#
# /Users/someone and /Users/example are conventional placeholders, the path
# equivalent of example.com: they name no real account. They are allowed by exact
# name only, so /Users/<any real name> still fails the review.
ALLOW=':[0-9]+:(127\.0\.0\.1(:[0-9]+)?|0\.0\.0\.0(:[0-9]+)?|::1(:[0-9]+)?|/Users/(someone|example)(/[^[:space:]]*)?)$'

cannot_scan() {
  echo "Hardwire check could not scan sources; review configured patterns." >&2
  return 2
}

# Search the NUL-delimited paths on stdin. "$@" is the search command; each
# batch is appended after "--". Exit 1 from the searcher means no match.
# Exit >1 means the scan did not run, which fails closed.
search_batches() {
  local -a cmd=("$@")
  local -a batch=()
  local path matches=""
  while IFS= read -r -d '' path; do
    [ -n "$path" ] || continue
    batch+=("$path")
    if [ "${#batch[@]}" -ge 400 ]; then
      run_batch || return 2
    fi
  done
  run_batch || return 2
  printf '%s' "$matches"
}

# Uses cmd, batch, and matches from search_batches (bash dynamic scope).
run_batch() {
  [ "${#batch[@]}" -eq 0 ] && return 0
  local status=0
  local out
  out=$("${cmd[@]}" -- "${batch[@]}" 2>/dev/null) || status=$?
  batch=()
  if [ "$status" -gt 1 ]; then
    cannot_scan
    return 2
  fi
  if [ "$status" -eq 0 ] && [ -n "$out" ]; then
    matches+="$out"$'\n'
  fi
}

# NUL paths on stdin, one relative path per line on stdout. A trailing
# `|| true` keeps an empty list from failing the script under `set -e`.
nul_to_lines() {
  local path rel
  while IFS= read -r -d '' path; do
    rel=${path#./}
    [ -n "$rel" ] || continue
    printf '%s\n' "$rel"
  done || true
}

# BSD mktemp (macOS) rejects a bare `mktemp`. A template works there, on GNU,
# and in Git Bash. The directory is removed on every exit from the walk.
hardwire_workdir() {
  mktemp -d "${TMPDIR:-/tmp}/tessera-hardwire.XXXXXX"
}

# Paths a source export should scan. find walks every regular file. rg --files
# drops gitignored paths without requiring a .git directory. `comm` keeps the
# paths both produced. That is a sorted-line intersection, not an associative
# array, so bash 3.2 can run it. The case statement repeats the pathspecs git
# grep uses inside a work tree.
export_paths() {
  command -v find >/dev/null 2>&1 || { cannot_scan; return 2; }
  command -v rg >/dev/null 2>&1 || { cannot_scan; return 2; }
  command -v sort >/dev/null 2>&1 || { cannot_scan; return 2; }
  command -v comm >/dev/null 2>&1 || { cannot_scan; return 2; }
  local work found allowed found_lines allowed_lines common rel status=0
  work=$(hardwire_workdir) || { cannot_scan; return 2; }
  found=$work/found
  allowed=$work/allowed
  found_lines=$work/found_lines
  allowed_lines=$work/allowed_lines
  common=$work/common
  find . \( -type d \( -name .git -o -name .hg -o -name .svn \) -prune \) -o -type f -print0 \
    > "$found" || status=$?
  if [ "$status" -ne 0 ]; then
    rm -rf "$work"
    cannot_scan
    return 2
  fi
  status=0
  rg --no-config --files --hidden --no-require-git --no-ignore-dot --no-ignore-parent --no-ignore-global \
    --glob '!.git/**' \
    --glob '!scripts/check_no_hardwires.sh' \
    --glob '!*.pyc' \
    --color never --null -- . \
    > "$allowed" 2>/dev/null || status=$?
  if [ "$status" -gt 1 ]; then
    rm -rf "$work"
    cannot_scan
    return 2
  fi
  status=0
  nul_to_lines < "$found" | LC_ALL=C sort -u > "$found_lines" || status=$?
  if [ "$status" -ne 0 ]; then
    rm -rf "$work"
    cannot_scan
    return 2
  fi
  status=0
  nul_to_lines < "$allowed" | LC_ALL=C sort -u > "$allowed_lines" || status=$?
  if [ "$status" -ne 0 ]; then
    rm -rf "$work"
    cannot_scan
    return 2
  fi
  status=0
  LC_ALL=C comm -12 "$found_lines" "$allowed_lines" > "$common" || status=$?
  if [ "$status" -ne 0 ]; then
    rm -rf "$work"
    cannot_scan
    return 2
  fi
  while IFS= read -r rel; do
    [ -n "$rel" ] || continue
    case "$rel" in
      scripts/check_no_hardwires.sh|*.pyc) continue ;;
    esac
    printf '%s\0' "$rel"
  done < "$common" || true
  rm -rf "$work"
}

scan_git() {
  local part="" status
  status=0
  part=$(git ls-files -z -- ':!scripts/check_no_hardwires.sh' ':!*.pyc' \
    | search_batches git grep -nIoE "$PATTERN") || status=$?
  if [ "$status" -ne 0 ]; then
    return "$status"
  fi
  local raw="$part"
  status=0
  part=$(git ls-files -z -- ':!scripts/check_no_hardwires.sh' ':!*.pyc' \
    | search_batches git grep --cached -nIoE "$PATTERN") || status=$?
  if [ "$status" -ne 0 ]; then
    return "$status"
  fi
  raw+=$'\n'"$part"
  status=0
  part=$(git ls-files --others --exclude-standard -z -- ':!scripts/check_no_hardwires.sh' ':!*.pyc' \
    | search_batches git grep --no-index -nIoE "$PATTERN") || status=$?
  if [ "$status" -ne 0 ]; then
    return "$status"
  fi
  raw+=$'\n'"$part"
  printf '%s' "$raw"
}

scan_export() {
  command -v find >/dev/null 2>&1 || { cannot_scan; return 2; }
  command -v rg >/dev/null 2>&1 || { cannot_scan; return 2; }
  local list status=0
  list=$(mktemp "${TMPDIR:-/tmp}/tessera-hardwire.XXXXXX") || { cannot_scan; return 2; }
  export_paths > "$list" || status=$?
  if [ "$status" -ne 0 ]; then
    rm -f "$list"
    return "$status"
  fi
  status=0
  search_batches rg --no-config -n -o -H --no-heading --color never -e "$PATTERN" < "$list" || status=$?
  rm -f "$list"
  return "$status"
}

if [ "$IN_WORK_TREE" -eq 1 ]; then
  RAW=$(scan_git) || exit $?
else
  RAW=$(scan_export) || exit $?
fi

HITS=$(printf '%s\n' "$RAW" | sed 's#^\./##' | grep -vE "$ALLOW" | grep . || true)
if [ -n "$HITS" ]; then
  echo "Hardwire check failed:" >&2
  # Public logs identify the source location without repeating private values.
  printf '%s\n' "$HITS" | cut -d: -f1,2 | sort -u >&2
  exit 1
fi
echo "Hardwire check passed."
