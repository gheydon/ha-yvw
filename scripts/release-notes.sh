#!/usr/bin/env bash
# Compose release notes for a tag: every change in it, in full, followed by the
# history of what came before.
#
# The commit messages say why each change was made, which is the part worth
# keeping. An auto-generated list of subject lines throws that away.
#
#   scripts/release-notes.sh v0.6.0
set -euo pipefail

tag="${1:?usage: release-notes.sh <tag>}"
previous="$(git describe --tags --abbrev=0 "${tag}^" 2>/dev/null || true)"

if [ -n "$previous" ]; then
    range="${previous}..${tag}"
else
    range="$tag"
fi

# The changes in this release, newest first, with the reasoning intact.
git log --no-merges --pretty=format:'## %s%n%n%b' "$range"

echo
echo

if [ -n "$previous" ]; then
    echo "**Full diff**: https://github.com/gheydon/ha-yvw/compare/${previous}...${tag}"
    echo
    echo "<details><summary>Everything before this</summary>"
    echo
    # Subject lines only: enough to see the shape of the project's history
    # without burying the release it belongs to.
    git log --no-merges --pretty=format:'- `%h` %s' "$previous" | sed 's/$//'
    echo
    echo
    echo "</details>"
fi
