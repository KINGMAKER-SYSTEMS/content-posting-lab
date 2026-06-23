#!/bin/sh
#
# install-git-hooks.sh -- copy the tracked git hooks into this repo's git common
# dir so they run for every worktree.
#
# Git hooks live under .git/hooks (the common dir, shared by all worktrees) and
# are NOT version controlled, so a fresh clone has none of them. The tracked
# source of truth lives next to this script (scripts/*.hook); run this once
# after cloning, or after a new worktree if the common hooks dir was reset.
#
# Currently installs:
#   post-checkout -> auto-stages services/openshot_bridge.py if it is present but
#   untracked, so the sanctioned OpenShot compiler module is never lost across
#   worktree transitions (see services/editor_render.py _MISSING_BRIDGE_MSG).
#
# ponytail: pure shell cp, no deps.

set -e

here=$(cd "$(dirname "$0")" && pwd)
hooks_dir="$(git rev-parse --git-common-dir)/hooks"
mkdir -p "$hooks_dir"

install -m 0755 "$here/post-checkout.hook" "$hooks_dir/post-checkout"
echo "installed post-checkout -> $hooks_dir/post-checkout"
