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

    if args.league == "nfl-debuts":
        print(f"set rookie season for {nfl.ingest_debuts(open_db())} NFL players")
        return
    if args.season is None:
        sys.exit("season is required, e.g. `cardtracker stats nfl 2025`")
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
    league = args.league.upper()
    kw = {"grader": args.grader, "grade": args.grade, "data": args.data}
    level = sz.SPARSE_LEVEL if args.data == "trends" else {}
    labels = {"player_index": "player indexes", "trends": "search interest, not prices"}
    label = labels.get(args.data, f"{args.grader or 'all graders'} {args.grade or ''}")
    print(f"{league} seasonality from {args.data} ({label.strip()})")
    _print_summaries("Change over each phase", sz.phase_returns(conn, league, **kw, **level))
    _print_summaries(f"Level vs. {args.event} date", sz.event_study(conn, league, args.event, **kw, **level))
    _print_summaries("Month vs. that year's median",
                     sz.month_of_year(conn, league, **kw, min_sales=level.get("min_sales", 2)))


def cmd_scp(args) -> None:
    from cardtracker.ingest import sportscardspro as scp

    conn = open_db()
    if args.action == "purge":
        if not args.yes:
            sys.exit("this deletes all SportsCardsPro data; re-run with --yes")
        print(scp.purge(conn))
        return
    client = scp.SportsCardsProClient()
    if args.action == "discover":
        players = []
        if args.nfl_top:
            players += [(r["name"], "football", r["debut_season"]) for r in conn.execute(
                # One row per player: stats are re-ingested daily, so a season has several as_of dates.
                "SELECT p.name, p.debut_season FROM players p JOIN player_stats s ON s.player_id = p.id "
                "WHERE p.sport = 'football' AND s.season = ? AND s.metric = 'fantasy_points_ppr' "
                "GROUP BY p.id ORDER BY MAX(s.value) DESC LIMIT ?", (args.season, args.nfl_top))]
        if args.nba_file:
            players += [(line.strip(), "basketball", None) for line in Path(args.nba_file).read_text().splitlines()
                        if line.strip() and not line.startswith("#")]
        missing = scp.build_watchlist(client, players, args.out)
        print(f"wrote {len(players) - len(missing)} cards to {args.out}")
        if missing:
            print(f"  no base Prizm card found for: {', '.join(missing)}")
        from cardtracker.watchlist import import_csv
        print(f"imported {import_csv(conn, args.out)} watchlist cards; "
              f"linked {scp.link_from_csv(conn, args.out)} new")
        removed = scp.drop_superseded(conn, args.out)
        if removed:
            print(f"removed {removed} previously discovered cards that this run replaced")
        print("now run `scp snapshot`")
        return
    if args.action == "link":
        linked, unmatched = scp.link_watchlist(conn, client)
        print(f"linked {linked} cards")
        for q in unmatched:
            print(f"  no unambiguous match: {q}")
    else:
        print(f"wrote {scp.snapshot(conn, client)} price rows")


def cmd_ch(args) -> None:
    from cardtracker.ingest import cardhedge as ch

    conn = open_db()
    client = ch.CardHedgeClient()
    if args.action == "link":
        linked, unmatched = ch.link_cards(conn, client)
        print(f"linked {linked} cards")
        for q in unmatched:
            print(f"  no unambiguous match: {q}")
    elif args.action == "history":
        grades = [g.strip() for g in args.grades.split(",")] if args.grades else None
        result = ch.fetch_price_history(conn, client, grades, max_calls=args.max_calls)
        print(" ".join(f"{k}={v}" for k, v in result.items()))
        if not result["complete"]:
            print("stopped at --max-calls; re-run to continue (finished windows are skipped)")
    elif args.action == "players":
        for sport in ("basketball", "football"):
            linked, unmatched = ch.link_players(conn, client, sport)
            print(f"{sport}: linked {linked} players" + (f"; unmatched: {', '.join(unmatched)}" if unmatched else ""))
        print(f"wrote {ch.fetch_player_index(conn, client)} player index points ({client.calls} calls)")


def cmd_trends_import(args) -> None:
    from cardtracker import trends

    conn = open_db()
    for path in args.csv:
        written, unmatched = trends.import_csv(conn, path, args.sport)
        print(f"{path}: {written} points")
        for term in unmatched:
            print(f"  '{term}' is not a known {args.sport} player; stored without a player link")


def _money(v: float | None) -> str:
    return f"${v:,.0f}" if v is not None and v >= 100 else (f"${v:,.2f}" if v is not None else "-")


def cmd_compare(args) -> None:
    from cardtracker.analytics import compare

    conn = open_db()
    day, rows = compare.card_rows(conn)
    if not rows:
        sys.exit("no price snapshots yet; run `scp snapshot` first")
    print(f"Card values as of {day} (SportsCardsPro)\n")
    head = f"{'player':<24}{'card':<16}{'raw':>9}{'9':>9}{'PSA 10':>10}{'10/raw':>8}{'10/9':>6}{'sales/yr':>9}"
    has_change = any(r.psa10_change_pct is not None for r in rows)
    print(head + ("  PSA10 chg" if has_change else ""))
    for sport in ("football", "basketball"):
        group = sorted((r for r in rows if r.sport == sport), key=lambda r: -(r.psa10 or 0))
        if not group:
            continue
        print(f"-- {sport} ({len(group)} cards)")
        for r in group:
            line = (f"{r.player[:23]:<24}{f'{r.year} #{r.card_number}':<16}{_money(r.raw):>9}{_money(r.grade9):>9}"
                    f"{_money(r.psa10):>10}{(f'{r.psa10_over_raw:.1f}x' if r.psa10_over_raw else '-'):>8}"
                    f"{(f'{r.psa10_over_9:.1f}x' if r.psa10_over_9 else '-'):>6}{(r.volume or 0):>9,}")
            if has_change:
                line += f"  {r.psa10_change_pct:+.1f}%" if r.psa10_change_pct is not None else "  -"
            print(line)

    ranked = compare.cheap_vs_performance(rows, compare.nfl_performance(conn, args.season))
    if ranked:
        print(f"\nNFL: PSA 10 price vs. {args.season} PPR points/game (standardized within position)")
        print("negative = cheaper than similarly performing peers; a research shortlist, not a buy signal")
        for r, v in ranked:
            print(f"  {r.player[:23]:<24}{(r.position or ''):<4}{_money(r.psa10):>10}  "
                  f"perf {v.performance:+.2f}  vs. fair {_money(v.fair_price):>8}  {v.residual:+.2f}")


def main(argv: list[str] | None = None) -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(prog="cardtracker")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="create the database schema").set_defaults(func=cmd_init_db)

    stats = sub.add_parser("stats", help="ingest player season stats")
    stats.add_argument("league", choices=["nba", "nfl", "nfl-debuts"],
                       help="nfl-debuts: rookie seasons for NFL players already loaded")
    stats.add_argument("season", type=int, nargs="?", help="year the season starts, e.g. 2025 for 2025-26")
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

    scp = sub.add_parser("scp", help="SportsCardsPro price guide (current values)").add_subparsers(
        dest="action", required=True)
    disc = scp.add_parser("discover", help="build a watchlist of each player's first Prizm base card")
    disc.add_argument("--nfl-top", type=int, default=0, help="top N NFL players by PPR points in --season")
    disc.add_argument("--season", type=int, default=2025)
    disc.add_argument("--nba-file", help="text file with one NBA player per line")
    disc.add_argument("--out", default="watchlists/generated.csv")
    disc.set_defaults(func=cmd_scp)
    scp.add_parser("link", help="match watchlist cards to SportsCardsPro products").set_defaults(func=cmd_scp)
    scp.add_parser("snapshot", help="record today's values for linked cards (run daily)").set_defaults(func=cmd_scp)
    purge = scp.add_parser("purge", help="delete all SportsCardsPro data (required when you cancel)")
    purge.add_argument("--yes", action="store_true")
    purge.set_defaults(func=cmd_scp)

    chp = sub.add_parser("ch", help="Card Hedge price history").add_subparsers(dest="action", required=True)
    chp.add_parser("link", help="match watchlist cards to Card Hedge cards").set_defaults(func=cmd_ch)
    hist = chp.add_parser("history", help="daily price history for linked cards, back to ~2020")
    hist.add_argument("--grades", help='comma-separated, default "PSA 10,PSA 9,Raw"')
    hist.add_argument("--max-calls", type=int, default=4500, help="stop after this many API calls")
    hist.set_defaults(func=cmd_ch)
    chp.add_parser("players", help="link watchlist players and fetch player index history").set_defaults(func=cmd_ch)

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

    cmp_ = sub.add_parser("compare", help="compare cards: grade multiples, liquidity, price vs. performance")
    cmp_.add_argument("--season", type=int, default=2025, help="NFL season for performance")
    cmp_.set_defaults(func=cmd_compare)

    tr = sub.add_parser("trends", help="Google Trends search interest").add_subparsers(dest="action", required=True)
    trimp = tr.add_parser("import", help="load Google Trends CSV exports (one player per file is best)")
    trimp.add_argument("csv", nargs="+")
    trimp.add_argument("--sport", required=True, choices=["basketball", "football"])
    trimp.set_defaults(func=cmd_trends_import)

    season = sub.add_parser("season", help="seasonality analysis").add_subparsers(dest="action", required=True)
    rep = season.add_parser("report", help="phase returns, event study, month-of-year")
    rep.add_argument("league", choices=["nfl", "nba"])
    rep.add_argument("--grader", help="e.g. PSA")
    rep.add_argument("--grade", type=float, help="e.g. 10")
    rep.add_argument("--event", default="playoffs_start")
    rep.add_argument("--data", default="sales", choices=["sales", "snapshots", "player_index", "trends"],
                     help="sales, daily price-guide values (e.g. Card Hedge history), player indexes, "
                          "or Google Trends search interest")
    rep.set_defaults(func=cmd_season_report)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
