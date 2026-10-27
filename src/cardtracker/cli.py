import argparse
import sys

from cardtracker.db import connect, db_path, init_db


def cmd_init_db(args) -> None:
    init_db(connect())
    print(f"initialized {db_path()}")


def cmd_stats(args) -> None:
    from cardtracker.stats import nba, nfl

    conn = connect()
    init_db(conn)
    module = {"nba": nba, "nfl": nfl}[args.league]
    rows = module.ingest_season(conn, args.season)
    print(f"{args.league} {args.season}: wrote {rows} stat rows")


def cmd_runs(args) -> None:
    conn = connect()
    init_db(conn)
    rows = conn.execute(
        "SELECT source, started_at, status, rows_written, detail FROM source_runs "
        "ORDER BY id DESC LIMIT ?", (args.limit,)
    ).fetchall()
    for r in rows:
        print(f"{r['started_at']}  {r['status']:<8} {r['rows_written']:>7}  {r['source']}"
              + (f"  ({r['detail']})" if r["detail"] else ""))
    if any(r["status"] != "ok" for r in rows):
        sys.exit(1)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="cardtracker")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="create the database schema").set_defaults(func=cmd_init_db)

    stats = sub.add_parser("stats", help="ingest player season stats")
    stats.add_argument("league", choices=["nba", "nfl"])
    stats.add_argument("season", type=int, help="year the season starts, e.g. 2025 for 2025-26")
    stats.set_defaults(func=cmd_stats)

    runs = sub.add_parser("runs", help="show recent ingestion runs; exits 1 if any are unhealthy")
    runs.add_argument("--limit", type=int, default=20)
    runs.set_defaults(func=cmd_runs)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
