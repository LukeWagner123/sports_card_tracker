#!/usr/bin/env bash
# Export the card data as JSON and commit it to cards/ in the PRIVATE studio repo, which the
# personal site's logged-in Studio reads (never the site's own repo: the data is licensed).
#
#   scripts/push_studio.sh                 # uses CARDTRACKER_DB and the default remote
#   STUDIO_DATA_TOKEN=... scripts/push_studio.sh   # in CI: a token that can write the studio repo
#
# Commits only when the data changed. Override the repo with STUDIO_DATA_REPO (owner/name).
set -euo pipefail
repo=${STUDIO_DATA_REPO:-LukeWagner123/studio}
if [ -n "${STUDIO_DATA_TOKEN:-}" ]; then
  remote="https://x-access-token:${STUDIO_DATA_TOKEN}@github.com/${repo}.git"
else
  remote="https://github.com/${repo}.git"
fi
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

cardtracker export studio --out "$work/export"
git clone -q --depth 1 "$remote" "$work/repo"
rm -rf "$work/repo/cards"
mkdir -p "$work/repo/cards"
cp "$work/export"/*.json "$work/repo/cards/"
cat > "$work/repo/cards/README.md" <<'MD'
Card market data for the Studio's Cards page, written by sports_card_tracker
(`scripts/push_studio.sh`). Licensed Card Hedge data: keep this repository private.
MD
cd "$work/repo"
git add -A cards
# meta.json carries a timestamp: commit only if something else changed too.
if git diff --cached --quiet -- . ':(exclude)cards/meta.json'; then
  echo "studio data unchanged"
  exit 0
fi
msg="Card data $(date -u +%F)"
[ -n "${STUDIO_COMMIT_FOOTER:-}" ] && msg=$(printf '%s\n\n%s' "$msg" "$STUDIO_COMMIT_FOOTER")
git -c user.name="cardtracker" -c user.email="cardtracker@users.noreply.github.com" commit -q -m "$msg"
git push -q origin HEAD
echo "pushed card data to ${repo} ($(du -sh cards | cut -f1))"
