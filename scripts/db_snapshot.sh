#!/usr/bin/env bash
# Save or restore the SQLite database as xz-compressed chunks (GitHub's file limit is 100 MB) on a
# branch that holds nothing else, replaced on every save so history doesn't grow.
#
#   scripts/db_snapshot.sh restore <db> [branch]   # fetch the branch; no branch yet = fresh start
#   scripts/db_snapshot.sh save    <db> [branch]   # force-push a single-commit snapshot
#
# The database holds licensed Card Hedge data: only ever push it to a PRIVATE repository.
set -euo pipefail
action=$1 db=$2 branch=${3:-data}
remote=${SNAPSHOT_REMOTE:-$(git remote get-url origin)}
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

case $action in
  restore)
    status=0
    git ls-remote --exit-code --heads "$remote" "$branch" >/dev/null || status=$?
    if [ "$status" = 2 ]; then
      echo "no '$branch' branch yet: starting a fresh database"
      exit 0
    elif [ "$status" != 0 ]; then
      echo "could not reach $remote" >&2
      exit 1
    fi
    git -C "$work" init -q
    git -C "$work" fetch -q --depth 1 "$remote" "$branch"
    git -C "$work" checkout -q FETCH_HEAD
    mkdir -p "$(dirname "$db")"
    cat "$work"/cards.db.xz.part-* | xz -d > "$db"
    echo "restored $db ($(du -h "$db" | cut -f1))"
    ;;
  save)
    python3 - "$db" <<'PY'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "database failed its integrity check"
c.execute("VACUUM")
PY
    xz -T0 -6 -c "$db" | split -b 90m - "$work/cards.db.xz.part-"
    cat > "$work/README.md" <<MD
Database snapshot written by scripts/db_snapshot.sh. Restore with:
    scripts/db_snapshot.sh restore data/cards.db
MD
    git -C "$work" init -q -b "$branch"
    git -C "$work" add .
    git -C "$work" -c user.name="cardtracker" -c user.email="cardtracker@users.noreply.github.com" \
      commit -q -m "Database snapshot $(date -u +%F)"
    git -C "$work" push -q -f "$remote" "$branch"
    echo "saved $db to '$branch' ($(du -ch "$work"/cards.db.xz.part-* | tail -1 | cut -f1) compressed)"
    ;;
  *)
    echo "usage: $0 restore|save <db> [branch]" >&2
    exit 2
    ;;
esac
