#!/bin/sh
# Point git at the tracked hook directory for this checkout.
#
# Why a script instead of a copied file: .git/hooks is not tracked, so a guard kept
# there disappears on a fresh clone and never reaches another machine. The hooks
# themselves live in scripts/git-hooks/ (tracked); this only tells git where to find
# them, via core.hooksPath, which is a per-checkout setting.
#
# Run once per clone. It is idempotent and prints what it changed.
set -eu

root=$(git rev-parse --show-toplevel)
hooks="$root/scripts/git-hooks"

if [ ! -d "$hooks" ]; then
    echo "install-git-hooks: $hooks is missing; run this from inside the repo." >&2
    exit 1
fi

for hook in "$hooks"/*; do
    [ -f "$hook" ] && chmod +x "$hook"
done

git config core.hooksPath scripts/git-hooks
echo "install-git-hooks: core.hooksPath = scripts/git-hooks"

# A copy in .git/hooks would now be ignored, and a stale copy is worse than none:
# it looks active while git runs the tracked version instead.
if [ -f "$root/.git/hooks/pre-commit" ]; then
    rm -f "$root/.git/hooks/pre-commit"
    echo "install-git-hooks: removed the untracked .git/hooks/pre-commit (it was inert)"
fi

echo "install-git-hooks: active hooks:"
for hook in "$hooks"/*; do
    [ -f "$hook" ] && echo "  $(basename "$hook")"
done
