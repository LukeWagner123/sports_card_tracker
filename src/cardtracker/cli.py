import argparse
import math
import os
import sys
from pathlib import Path

from cardtracker.db import connect, db_path, init_db


def load_dotenv(path: str = ".env") -> None:
    """Minimal .env loader; values already in the environment win."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


def open_db():
    conn = connect()
    init_db(conn)
    return conn


def cmd_init_db(args) -> None:
    open_db()
    print(f"initialized {db_path()}")


def cmd_stats(args) -> None:
    from cardtracker.stats import nba, nfl

    module = {"nba": nba, "nfl": nfl}[args.league]
    rows = module.ingest_season(open_db(), args.season)
    print(f"{args.league} {args.season}: wrote {rows} stat rows")


def cmd_runs(args) -> None:
    rows = open_db().execute(
        "SELECT source, started_at, status, rows_written, detail FROM source_runs "
        "ORDER BY id DESC LIMIT ?", (args.limit,)
    ).fetchall()
    for r in rows:
        print(f"{r['started_at']}  {r['status']:<8} {r['rows_written']:>7}  {r['source']}"
              + (f"  ({r['detail']})" if r["detail"] else ""))
    if any(r["status"] not in ("ok", "idle") for r in rows):
        sys.exit(1)


def cmd_watch_import(args) -> None:
    from cardtracker.watchlist import import_csv

    print(f"imported {import_csv(open_db(), args.csv)} watchlist cards")


def cmd_watch_list(args) -> None:
    rows = open_db().execute(
        "SELECT p.name, c.year, c.set_name, c.card_number, c.parallel, w.search_query, w.last_searched "
        "FROM watchlist w JOIN cards c ON c.id = w.card_id JOIN players p ON p.id = c.player_id "
        "ORDER BY p.sport, p.name"
    ).fetchall()
    for r in rows:
        print(f"{r['name']:<24} {r['year']} {r['set_name']} #{r['card_number']} {r['parallel']:<8} "
              f"q={r['search_query']!r}  last={r['last_searched'] or '-'}")


def cmd_ebay_poll(args) -> None:
    from cardtracker.ingest.ebay_auctions import poll
    from cardtracker.ingest.ebay_browse import EbayBrowseClient

    result = poll(open_db(), EbayBrowseClient())
    print(" ".join(f"{k}={v}" for k, v in result.items()))


def cmd_calendar(args) -> None:
    from cardtracker import calendar

    conn = open_db()
    if args.league == "nfl":
        n = calendar.ingest_nfl(conn)
    else:
        n = calendar.ingest_nba(conn, args.first, args.last)
    print(f"{args.league}: wrote {n} season events")


def cmd_calendar_import(args) -> None:
    from cardtracker import calendar

    print(f"imported {calendar.import_csv(open_db(), args.csv)} season events")


def cmd_prices_import(args) -> None:
    from cardtracker.prices import import_csv

    imported, missing = import_csv(open_db(), args.csv)
    print(f"imported {imported} sales")
    for m in sorted(set(missing)):
        print(f"  no matching card (add it to a watchlist first): {m}")


def _print_summaries(title: str, rows) -> None:
    print(f"\n{title}")
    if not rows:
        print("  (not enough data)")
        return
    print(f"  {'':<14}{'median':>9}{'hit rate':>10}{'n':>7}   median by season")
    for r in rows:
        seasons = "  ".join(f"{s}:{math.expm1(v) * 100:+.0f}%" for s, v in r.by_season.items())
        print(f"  {r.key:<14}{r.median_pct:>+8.1f}%{r.hit_rate * 100:>9.0f}%{r.n:>7}   {seasons}")


def cmd_season_report(args) -> None:
    from cardtracker.analytics import seasonality as sz

    conn = open_db()
    league, kw = args.league.upper(), {"grader": args.grader, "grade": args.grade}
    print(f"{league} seasonality ({args.grader or 'all graders'} {args.grade or ''})".rstrip())
    _print_summaries("Return over each phase", sz.phase_returns(conn, league, **kw))
    _print_summaries(f"Price vs. {args.event} date", sz.event_study(conn, league, args.event, **kw))
    _print_summaries("Month vs. that year's median", sz.month_of_year(conn, league, **kw))


def main(argv: list[str] | None = None) -> None:
    load_dotenv()
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

    watch = sub.add_parser("watch", help="manage the card watchlist").add_subparsers(dest="action", required=True)
    imp = watch.add_parser("import", help="load cards from a watchlist CSV")
    imp.add_argument("csv")
    imp.set_defaults(func=cmd_watch_import)
    watch.add_parser("list", help="show watchlist cards").set_defaults(func=cmd_watch_list)

    ebay = sub.add_parser("ebay", help="eBay Browse API collection").add_subparsers(dest="action", required=True)
    ebay.add_parser("poll", help="observe watchlist auctions; run every ~10 minutes").set_defaults(func=cmd_ebay_poll)

    cal = sub.add_parser("calendar", help="league season dates").add_subparsers(dest="action", required=True)
    fetch = cal.add_parser("fetch", help="fetch season dates (NFL: nflverse; NBA: stats.nba.com, run from home)")
    fetch.add_argument("league", choices=["nfl", "nba"])
    fetch.add_argument("--first", type=int, default=2015, help="NBA only: first season")
    fetch.add_argument("--last", type=int, default=2025, help="NBA only: last season")
    fetch.set_defaults(func=cmd_calendar)
    cimp = cal.add_parser("import", help="load season events (draft, trade deadline, ...) from CSV")
    cimp.add_argument("csv")
    cimp.set_defaults(func=cmd_calendar_import)

    prices = sub.add_parser("prices", help="historical sales").add_subparsers(dest="action", required=True)
    pimp = prices.add_parser("import", help="load historical sales from CSV")
    pimp.add_argument("csv")
    pimp.set_defaults(func=cmd_prices_import)

    season = sub.add_parser("season", help="seasonality analysis").add_subparsers(dest="action", required=True)
    rep = season.add_parser("report", help="phase returns, event study, month-of-year")
    rep.add_argument("league", choices=["nfl", "nba"])
    rep.add_argument("--grader", help="e.g. PSA")
    rep.add_argument("--grade", type=float, help="e.g. 10")
    rep.add_argument("--event", default="playoffs_start")
    rep.set_defaults(func=cmd_season_report)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
