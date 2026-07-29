#!/usr/bin/env bash
#
# Publish wiki/*.md to this repo's GitHub Wiki tab.
#
#   ./wiki/sync-to-github-wiki.sh
#
# The wiki is a SEPARATE git repo (<repo>.wiki.git) with a flat URL space, so
# the pages need two mechanical rewrites that this script applies to the copies
# it pushes. The files in wiki/ are left untouched — their relative links are
# correct for browsing the repo tree.
#
#   1. `](Page-Name.md)` -> `](Page-Name)`   wiki URLs have no .md
#   2. `` `../path/file` `` -> a link to that file on github.com — there is no
#      parent directory to reach from inside the wiki
#
# ONE-TIME SETUP: GitHub does not create the wiki git repo until the first page
# exists, and there is no API for wiki content. Before the first run, open
#   https://github.com/<owner>/<repo>/wiki
# and save any page (its content gets overwritten by this script).
set -euo pipefail

cd "$(dirname "$0")/.."
SRC="$PWD/wiki"

SLUG=$(git config --get remote.origin.url \
  | sed -E 's#(git@github\.com:|https://github\.com/)##; s#\.git$##')
[ -n "$SLUG" ] || { echo "no github origin remote found" >&2; exit 1; }

BLOB="https://github.com/$SLUG/blob/main"
WIKI_URL="https://github.com/$SLUG.wiki.git"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

# gh's credential helper avoids the macOS Keychain, which is unreachable from
# some sandboxed/non-interactive shells. A function, not a variable — the
# helper string contains spaces and would word-split.
git_auth() { git -c credential.helper='!gh auth git-credential' "$@"; }

if ! git_auth clone -q "$WIKI_URL" "$TMP/wiki" 2>/dev/null; then
  cat >&2 <<EOF
Could not clone $WIKI_URL

The wiki repo does not exist yet. GitHub creates it only after the first page
is saved through the web UI, and offers no API for it. Open

    https://github.com/$SLUG/wiki

save any page, then re-run this script — it will overwrite that page.
EOF
  exit 1
fi

for f in "$SRC"/*.md; do
  sed -E \
    -e 's|\]\(([A-Za-z0-9._-]+)\.md([)#])|](\1\2|g' \
    -e "s#\`\.\./([^\`]+)\`#[\`\1\`]($BLOB/\1)#g" \
    "$f" > "$TMP/wiki/$(basename "$f")"
done

# Sidebar renders on every page. Home is the landing page and needs no entry.
cat > "$TMP/wiki/_Sidebar.md" <<'EOF'
### cardputer + Claude MCP

- [Home](Home)
- [Getting Started](Getting-Started)
- [Using It](Using-It)
- [Protocol Reference](Protocol-Reference)
- [Troubleshooting](Troubleshooting)
EOF

cd "$TMP/wiki"
git add -A
if git diff --cached --quiet; then
  echo "wiki already up to date"
  exit 0
fi
git commit -q -m "Sync wiki pages from wiki/ in the main repo"
git_auth push -q origin HEAD 2>&1 | grep -vE '^failed to (get|store)' || true
echo "pushed: https://github.com/$SLUG/wiki"
