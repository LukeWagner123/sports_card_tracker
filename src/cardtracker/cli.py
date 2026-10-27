import argparse

from cardtracker.db import connect, db_path, init_db


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="cardtracker")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init-db", help="create the database schema")
    args = parser.parse_args(argv)

    if args.command == "init-db":
        conn = connect()
        init_db(conn)
        print(f"initialized {db_path()}")


if __name__ == "__main__":
    main()
