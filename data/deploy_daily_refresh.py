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


def main():
    sql_file = (Path(sys.argv[1]) if len(sys.argv) > 1
                else REPO_ROOT / "data" / "setup_daily_refresh.sql")
    conn = snowflake.connector.connect(
        connection_name=os.environ.get("SNOWFLAKE_CONNECTION_NAME", "HE20264")
    )
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
