"""Snowflake link: one cached connection, CALL ANSWER_FARMER.

The proc does ALL the reasoning (live weather, tiered prices, MSP, Cortex
Search, Cortex COMPLETE). This module never computes advice itself.
"""

import json
import os
import threading

import snowflake.connector

_lock = threading.Lock()
_conn = None


def _connect():
    # Key-pair auth: no browser, no token expiry — required for a headless
    # server (the OAuth connection pops login tabs). Falls back to the
    # interactive OAuth connection if no key is configured.
    #
    # Serverless (e.g. Vercel) has no persistent filesystem for the key
    # file, so SF_PRIVATE_KEY may hold the PEM content directly — written
    # once to /tmp per cold start and reused via SF_PRIVATE_KEY_PATH from
    # there on.
    key_path = os.environ.get("SF_PRIVATE_KEY_PATH")
    key_pem = os.environ.get("SF_PRIVATE_KEY")
    if key_pem and not (key_path and os.path.exists(key_path)):
        key_path = "/tmp/sf_key.p8"
        if not os.path.exists(key_path):
            with open(key_path, "w") as f:
                f.write(key_pem)
    if key_path and os.path.exists(key_path):
        conn = snowflake.connector.connect(
            account=os.environ.get("SF_ACCOUNT", "bm13081.ap-southeast-2"),
            user=os.environ.get("SF_USER", "NIKKY001"),
            private_key_file=key_path)
    else:
        conn = snowflake.connector.connect(
            connection_name=os.environ.get("SNOWFLAKE_CONNECTION_NAME", "HE20264"))
    cur = conn.cursor()
    cur.execute("USE WAREHOUSE KISAN_WH")
    cur.execute("USE SCHEMA AGRI.PUBLIC")
    return conn


def _get_conn():
    global _conn
    with _lock:
        if _conn is None or _conn.is_closed():
            _conn = _connect()
        return _conn


def answer_farmer(district: str, crop: str, question: str, language: str) -> dict:
    global _conn
    for attempt in (1, 2):  # one retry: OAuth token expiry closes connections
        try:
            cur = _get_conn().cursor()
            cur.execute("CALL AGRI.PUBLIC.ANSWER_FARMER(%s, %s, %s, %s)",
                        (district, crop, question, language))
            return json.loads(cur.fetchone()[0])
        except snowflake.connector.errors.Error:
            with _lock:
                try:
                    _conn.close()
                except Exception:
                    pass
                _conn = None
            if attempt == 2:
                raise


def query(sql: str, params=None) -> list[dict]:
    """Read-only helper for the browse endpoints (/prices, /guide, /snapshot)."""
    global _conn
    for attempt in (1, 2):
        try:
            cur = _get_conn().cursor()
            cur.execute(sql, params or ())
            cols = [d[0].lower() for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
        except snowflake.connector.errors.Error:
            with _lock:
                try:
                    _conn.close()
                except Exception:
                    pass
                _conn = None
            if attempt == 2:
                raise


def ping() -> bool:
    cur = _get_conn().cursor()
    cur.execute("SELECT 1")
    return cur.fetchone()[0] == 1
