#!/usr/bin/env bash
# Stage an older AetherMind pin into a self-contained build tree.
#
# The release wheel does not use this script. It vendors
# https://github.com/MaverickKB/aethermind-engine at
# ec1011eebe00e8197e29acbbb3ae710090f53899 and checks that tree against
# docs/packaging/aethermind-engine-manifest.json. This script still verifies
# the older manifest when an operator has a copy of that pin.
#
# The provenance manifest says WHICH bytes are the engine;
# this script materializes them. It takes the pinned bytes from one of three
# sources, verifies the staged copy byte-for-byte against the manifest, and
# writes a build receipt next to the code. It does not vendor code into the
# repository.
#
# Usage:
#   TESSERA_ENGINE_ROOT=/path/to/engine/tree scripts/build-aethermind-bundle.sh
#   TESSERA_ENGINE_ROOT=/path TESSERA_PACKAGING_HOST=otherhost \
#       TESSERA_BUILD_DIR=/output/dir scripts/build-aethermind-bundle.sh
#
# Sources (TESSERA_ENGINE_SOURCE, default "host"):
#   host     pull the pinned files from TESSERA_PACKAGING_HOST over ssh, rooted
#            at TESSERA_ENGINE_ROOT. This is the default. It only works while
#            that host still holds the pinned bytes.
#   dir      copy from a local engine tree, TESSERA_ENGINE_ROOT on this machine.
#            Use this when the serving host has drifted but a verified copy of
#            the pin survives locally.
#   archive  read the pinned files out of a tarball, TESSERA_ENGINE_ARCHIVE.
#            Use this to rebuild from the provenance archive kept off-machine.
#            The operator supplies the location; it is deliberately not named
#            here, because a deployment path does not belong in the shipped tree.
#            The archive may wrap the tree in one top-level directory or hold it
#            at the root; both layouts are detected.
#
# The archive source lets a fresh clone rebuild from a tarball the operator
# supplies, without a host that still holds the pin. It does not decide
# whether the bytes belong in git.
#
# The build directory defaults to <repo>/.tessera-build/aethermind-engine/.
# Re-running replaces it. The receipt records the manifest hash, the file count
# and the build clock; verify-aethermind-manifest.sh stays the ongoing check.
set -euo pipefail

SOURCE="${TESSERA_ENGINE_SOURCE:-host}"
HOST="${TESSERA_PACKAGING_HOST:-}"
ROOT="${TESSERA_ENGINE_ROOT:-}"
ARCHIVE="${TESSERA_ENGINE_ARCHIVE:-}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
MANIFEST="$REPO/docs/packaging/aethermind-engine-manifest-20261023.json"
BUILD_DIR="${TESSERA_BUILD_DIR:-$REPO/.tessera-build/aethermind-engine}"

if [ ! -f "$MANIFEST" ]; then
  echo "build-aethermind-bundle: manifest not found: $MANIFEST" >&2
  exit 2
fi

case "$SOURCE" in
  host)
    if [ -z "$HOST" ]; then
      echo "build-aethermind-bundle: TESSERA_PACKAGING_HOST is not set." >&2
      echo "Set it to the machine that holds the engine tree, or use TESSERA_ENGINE_SOURCE=dir for a tree on this machine." >&2
      exit 2
    fi
    if [ -z "$ROOT" ]; then
      echo "build-aethermind-bundle: TESSERA_ENGINE_ROOT is not set." >&2
      echo "Set it to the engine tree directory on $HOST (the folder holding lib/ and mcp/)." >&2
      exit 2
    fi
    ;;
  dir)
    if [ -z "$ROOT" ]; then
      echo "build-aethermind-bundle: TESSERA_ENGINE_ROOT is not set." >&2
      echo "Set it to the local engine tree directory (the folder holding lib/ and mcp/)." >&2
      exit 2
    fi
    if [ ! -d "$ROOT" ]; then
      echo "build-aethermind-bundle: TESSERA_ENGINE_ROOT is not a directory: $ROOT" >&2
      exit 2
    fi
    ;;
  archive)
    if [ -z "$ARCHIVE" ]; then
      echo "build-aethermind-bundle: TESSERA_ENGINE_ARCHIVE is not set." >&2
      echo "Set it to a tarball of the pinned engine tree." >&2
      exit 2
    fi
    if [ ! -f "$ARCHIVE" ]; then
      echo "build-aethermind-bundle: TESSERA_ENGINE_ARCHIVE not found: $ARCHIVE" >&2
      exit 2
    fi
    ;;
  *)
    echo "build-aethermind-bundle: unknown TESSERA_ENGINE_SOURCE: $SOURCE (host|dir|archive)" >&2
    exit 2
    ;;
esac

# The manifest lists every pinned file. Pull exactly those bytes.
paths="$(python3 - "$MANIFEST" <<'PY'
import json, sys
manifest = json.load(open(sys.argv[1]))
print(" ".join("'%s'" % path for path in manifest["files"]))
PY
)"

stage="$REPO/.tessera-build/aethermind-engine-stage"
rm -rf "$stage" "$BUILD_DIR"
mkdir -p "$stage" "$BUILD_DIR"

# Refuse before copying if the source cannot satisfy this manifest.
#
# A missing pinned member means the source is STALE against the manifest, which
# happens whenever the manifest is revised (for example a re-pin that renames a
# module). tar reports that only through its exit status, which an eval'd command
# list swallows, so a missing member has to be named here before a half-built
# tree fails verification.
require_all_pinned() {
  # $1 is a directory holding the manifest's relative paths.
  missing=""
  for path in $paths; do
    name="$(printf '%s' "$path" | tr -d "'")"
    if [ ! -f "$1/$name" ]; then missing="$missing $name"; fi
  done
  if [ -n "$missing" ]; then
    echo "build-aethermind-bundle: the $SOURCE source does not carry every file this manifest pins." >&2
    echo "build-aethermind-bundle: missing:$missing" >&2
    echo "build-aethermind-bundle: it is stale against $(basename "$MANIFEST"); use a source that matches the current manifest." >&2
    rm -rf "$stage" "$BUILD_DIR"
    exit 1
  fi
}

case "$SOURCE" in
  host)
    # A remote tree cannot be inspected without a second ssh, so the tar stream
    # is verified after extraction below; the local check does not apply.
    ssh "$HOST" "cd \"$ROOT\" && tar czf - $paths" > "$stage/engine.tar.gz"
    ;;
  dir)
    require_all_pinned "$ROOT"
    ( cd "$ROOT" && eval "tar czf '$stage/engine.tar.gz' $paths" )
    ;;
  archive)
    # Extract the archive, then re-pack only the pinned members. Re-packing is what
    # keeps a convenience tarball (one that also carries a venv or cache residue)
    # from leaking into the build tree; the extraction layout only decides where
    # the manifest's relative paths start.
    #
    # The provenance archive wraps the tree in one top-level directory
    # ("aethermind-engine/"), while a tree tarred from its own root does not.
    # Detect which layout this is instead of assuming either.
    prefix="$(tar tzf "$ARCHIVE" | head -1 | sed -e 's|^\./||' -e 's|/.*$||')"
    if tar tzf "$ARCHIVE" | grep -qx "$prefix/lib/" 2>/dev/null; then
      tar xzf "$ARCHIVE" -C "$stage" --strip-components=1
    else
      tar xzf "$ARCHIVE" -C "$stage"
    fi
    require_all_pinned "$stage"
    ( cd "$stage" && eval "tar czf engine.tar.gz $paths" )
    ;;
esac

tar -xzf "$stage/engine.tar.gz" -C "$BUILD_DIR"
rm -rf "$stage"

# Local verification: every pinned file in the build tree must match byte-for-byte.
python3 - "$MANIFEST" "$BUILD_DIR" "$SOURCE" <<'PY'
import hashlib, json, os, sys
manifest = json.load(open(sys.argv[1]))
build_dir = sys.argv[2]
source = sys.argv[3]
checked = 0
for relpath in sorted(manifest["files"]):
    target = os.path.join(build_dir, relpath)
    if not os.path.isfile(target):
        print(f"build-aethermind-bundle: MISSING in build tree: {relpath}", file=sys.stderr)
        sys.exit(1)
    digest = hashlib.sha256(open(target, "rb").read()).hexdigest()
    if digest != manifest["files"][relpath]:
        print(f"build-aethermind-bundle: MISMATCH: {relpath}", file=sys.stderr)
        sys.exit(1)
    checked += 1

receipt = {
    "built_from_manifest": "aethermind-engine-manifest-20261023.json",
    "files": checked,
    "source": source,
    # This script compares the staged tree against the manifest only. It does not
    # reach the serving host, so it cannot claim the bytes still match what that
    # host runs today -- and they need not: the pin records the bytes the bundle
    # was cut from, which is the point of pinning. Say only what was checked.
    "verdict": "all manifest-pinned files byte-identical to the staged source tree",
}
import datetime
receipt["built_at"] = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
json.dump(receipt, open(os.path.join(build_dir, "build-receipt.json"), "w"), indent=2)
print(f"build-aethermind-bundle: {checked} files staged and verified in {build_dir} (source: {source})")
PY
