import os
import sqlite3

import psycopg2


SQLITE_DB_FILE = os.environ.get("SQLITE_DB_FILE", "orders.db")
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

TABLES = [
    "daily_counters",
    "daily_order_counters",
    "call_state",
    "orders",
    "order_tickets",
    "sessions",
    "soldout",
    "inventory",
    "queue_tickets",
    "queue_line_bindings",
]

ID_TABLES = [
    "orders",
    "order_tickets",
    "inventory",
    "queue_tickets",
    "queue_line_bindings",
]


def quote_ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def table_exists(sqlite_conn, table: str) -> bool:
    row = sqlite_conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return bool(row)


def sqlite_columns(sqlite_conn, table: str) -> list[str]:
    return [row[1] for row in sqlite_conn.execute(f"PRAGMA table_info({quote_ident(table)})")]


def copy_table(sqlite_conn, pg_conn, table: str) -> int:
    if not table_exists(sqlite_conn, table):
        return 0

    columns = sqlite_columns(sqlite_conn, table)
    if not columns:
        return 0

    rows = sqlite_conn.execute(
        f"SELECT {', '.join(quote_ident(c) for c in columns)} FROM {quote_ident(table)}"
    ).fetchall()
    if not rows:
        return 0

    placeholders = ", ".join(["%s"] * len(columns))
    col_sql = ", ".join(quote_ident(c) for c in columns)
    insert_sql = f"INSERT INTO {quote_ident(table)} ({col_sql}) VALUES ({placeholders}) ON CONFLICT DO NOTHING"

    with pg_conn.cursor() as cur:
        cur.executemany(insert_sql, rows)
    return len(rows)


def reset_sequence(pg_conn, table: str):
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            SELECT setval(
                pg_get_serial_sequence(%s, 'id'),
                GREATEST(COALESCE((SELECT MAX(id) FROM %s), 0), 1),
                COALESCE((SELECT MAX(id) FROM %s), 0) > 0
            )
            """
            % ("%s", quote_ident(table), quote_ident(table)),
            (table,),
        )


def main():
    if not DATABASE_URL:
        raise SystemExit("DATABASE_URL is required")
    if not os.path.exists(SQLITE_DB_FILE):
        raise SystemExit(f"SQLite file not found: {SQLITE_DB_FILE}")

    sqlite_conn = sqlite3.connect(SQLITE_DB_FILE)
    pg_conn = psycopg2.connect(DATABASE_URL)

    try:
        total = 0
        for table in TABLES:
            count = copy_table(sqlite_conn, pg_conn, table)
            total += count
            print(f"{table}: copied {count} rows")

        for table in ID_TABLES:
            reset_sequence(pg_conn, table)

        pg_conn.commit()
        print(f"Done. Total copied rows: {total}")
    except Exception:
        pg_conn.rollback()
        raise
    finally:
        sqlite_conn.close()
        pg_conn.close()


if __name__ == "__main__":
    main()
