"""Apply the CockroachDB schema.

Usage:
    python db/migrate.py                 # apply cluster settings + schema
    python db/migrate.py --schema-only   # skip cluster settings (no admin rights)

Reads COCKROACH_DATABASE_URL from the environment, or from a .env file at the
repo root if the variable is unset.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import psycopg

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_DIR = REPO_ROOT / "db"


def load_env() -> None:
    """Populate os.environ from .env for values that aren't already set.

    Deliberately minimal — a real .env parser is a dependency we don't need for
    three variables, and the deployed Lambdas get their config from the
    environment directly.
    """
    env_file = REPO_ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def apply_sql(conn: psycopg.Connection, path: Path) -> None:
    sql = path.read_text(encoding="utf-8")
    with conn.cursor() as cur:
        cur.execute(sql)
    print(f"  applied {path.name}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--schema-only",
        action="store_true",
        help="skip 00_cluster_settings.sql (requires admin on the cluster)",
    )
    args = parser.parse_args()

    load_env()
    url = os.environ.get("COCKROACH_DATABASE_URL")
    if not url:
        print(
            "COCKROACH_DATABASE_URL is not set. Copy .env.example to .env and "
            "fill it in, or export the variable.",
            file=sys.stderr,
        )
        return 1

    # autocommit because CockroachDB applies cluster settings outside a txn,
    # and schema.sql manages its own BEGIN/COMMIT.
    with psycopg.connect(url, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT version()")
            print(f"connected: {cur.fetchone()[0].split(' (')[0]}")

        if not args.schema_only:
            try:
                apply_sql(conn, DB_DIR / "00_cluster_settings.sql")
            except psycopg.Error as e:
                print(
                    f"  cluster settings FAILED: {e}\n"
                    "  Vector indexes are gated behind this setting. Ask a "
                    "cluster admin to run db/00_cluster_settings.sql, then "
                    "re-run with --schema-only.",
                    file=sys.stderr,
                )
                return 1

        apply_sql(conn, DB_DIR / "schema.sql")

        with conn.cursor() as cur:
            cur.execute("""
                SELECT table_name FROM information_schema.tables
                WHERE table_schema = 'public' ORDER BY table_name
            """)
            tables = [r[0] for r in cur.fetchall()]
        print(f"\n{len(tables)} tables: {', '.join(tables)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
