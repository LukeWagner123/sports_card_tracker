#!/usr/bin/env bash
# Save or restore the SQLite database as xz-compressed chunks (GitHub's file limit is 100 MB) on a
# branch that holds nothing else, replaced on every save so history doesn't grow.
#
#   scripts/db_snapshot.sh restore <db> [branch]   # fetch the branch; no branch yet = fresh start
#   scripts/db_snapshot.sh save    <db> [branch]   # force-push a single-commit snapshot
#
# The database holds licensed Card Hedge data, so snapshots are always encrypted (AES-256,
# PBKDF2) with SNAPSHOT_KEY, or CARDHEDGE_API_KEY if that is unset: a copy that leaks, or a repo
# that is public for a while, exposes only ciphertext. Without a key, nothing is saved.
set -euo pipefail
action=$1 db=$2 branch=${3:-data}
key=${SNAPSHOT_KEY:-${CARDHEDGE_API_KEY:-}}
if [ -z "$key" ]; then
  echo "set SNAPSHOT_KEY or CARDHEDGE_API_KEY: snapshots are encrypted with it" >&2
  exit 2
fi
export SNAPSHOT_PASS=$key
crypt() { openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt -pass env:SNAPSHOT_PASS "$@"; }
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
    if ! cat "$work"/cards.db.xz.enc.part-* | crypt -d 2>/dev/null | xz -d > "$db.tmp"; then
      rm -f "$db.tmp"
      echo "could not decrypt the snapshot: wrong SNAPSHOT_KEY / CARDHEDGE_API_KEY?" >&2
      exit 1
    fi
    mv "$db.tmp" "$db"
    echo "restored $db ($(du -h "$db" | cut -f1))"
    ;;
  save)
    # Snapshot a consistent copy (SQLite backup API), so saving while a pull is writing is safe.
    mkdir -p "$work/copy"
    python3 - "$db" "$work/copy/cards.db" <<'PY'
import sqlite3, sys
src = sqlite3.connect(sys.argv[1], timeout=120)
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)
assert dst.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "database failed its integrity check"
dst.execute("VACUUM")
dst.close()
PY
    xz -T0 -6 -c "$work/copy/cards.db" | crypt | split -b 90m - "$work/cards.db.xz.enc.part-"
    rm -rf "$work/copy"
    cat > "$work/README.md" <<MD
Encrypted database snapshot written by scripts/db_snapshot.sh. Restore with:
    CARDHEDGE_API_KEY=... scripts/db_snapshot.sh restore data/cards.db
MD
    git -C "$work" init -q -b "$branch"
    git -C "$work" add .
    git -C "$work" -c user.name="cardtracker" -c user.email="cardtracker@users.noreply.github.com" \
      commit -q -m "Database snapshot $(date -u +%F)"
    git -C "$work" push -q -f "$remote" "$branch"
    echo "saved $db to '$branch' ($(du -ch "$work"/cards.db.xz.enc.part-* | tail -1 | cut -f1) encrypted)"
    ;;
  *)
    echo "usage: $0 restore|save <db> [branch]" >&2
    exit 2
    ;;
esac
