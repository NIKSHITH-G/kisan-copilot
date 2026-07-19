#!/usr/bin/env python3
"""Deploy a setup SQL file to Snowflake statement by statement.

Usage: deploy_daily_refresh.py [path/to/file.sql]   (default: setup_daily_refresh.sql)
"""

import os
import sys
from pathlib import Path

import snowflake.connector
from snowflake.connector.util_text import split_statements

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_env():
    env = REPO_ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                if v.strip() and k.strip() not in os.environ:
                    os.environ[k.strip()] = v.strip().strip("'\"")


def connect():
    """Key-pair first (no browser, no token expiry); OAuth connection fallback."""
    _load_env()
    key_path = os.environ.get("SF_PRIVATE_KEY_PATH")
    if key_path and (REPO_ROOT / key_path).exists():
        return snowflake.connector.connect(
            account=os.environ.get("SF_ACCOUNT", "bm13081.ap-southeast-2"),
            user=os.environ.get("SF_USER", "NIKKY001"),
            private_key_file=str(REPO_ROOT / key_path))
    return snowflake.connector.connect(
        connection_name=os.environ.get("SNOWFLAKE_CONNECTION_NAME", "HE20264"))


def main():
    sql_file = (Path(sys.argv[1]) if len(sys.argv) > 1
                else REPO_ROOT / "data" / "setup_daily_refresh.sql")
    conn = connect()
    cur = conn.cursor()
    try:
        with sql_file.open() as f:
            for stmt, _ in split_statements(f, remove_comments=True):
                stmt = stmt.strip().rstrip(";")
                if not stmt:
                    continue
                first_line = stmt.splitlines()[0][:80]
                cur.execute(stmt)
                print(f"OK  {first_line}  ->  {cur.fetchone()[0]}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
