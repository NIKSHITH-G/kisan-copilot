-- Daily auto-refresh of live weather + mandi prices, fully inside Snowflake.
-- Deploy with: .venv/bin/python data/deploy_daily_refresh.py
--
-- Pieces:
--   1. NETWORK RULE + EXTERNAL ACCESS INTEGRATION -> lets Snowflake Python call
--      api.open-meteo.com and api.data.gov.in directly.
--   2. SECRET data_gov_key -> holds the data.gov.in API key (placeholder until set):
--        ALTER SECRET AGRI.PUBLIC.DATA_GOV_KEY SET SECRET_STRING = '<real key>';
--   3. PROCEDURE REFRESH_LIVE_DATA() -> same fetch+MERGE logic as
--      data/fetch_live_data.py, running inside Snowflake.
--   4. TASK DAILY_REFRESH_TASK -> calls it every day at 18:30 IST.

USE ROLE ACCOUNTADMIN;
USE WAREHOUSE KISAN_WH;
USE SCHEMA AGRI.PUBLIC;

CREATE NETWORK RULE IF NOT EXISTS AGRI.PUBLIC.AGRI_APIS_NETWORK_RULE
  MODE = EGRESS
  TYPE = HOST_PORT
  VALUE_LIST = ('api.open-meteo.com', 'geocoding-api.open-meteo.com', 'api.data.gov.in');

CREATE SECRET IF NOT EXISTS AGRI.PUBLIC.DATA_GOV_KEY
  TYPE = GENERIC_STRING
  SECRET_STRING = 'PENDING';

-- One row per refresh run; task history does not keep the proc's return
-- string, so failures inside the proc would otherwise be invisible.
CREATE TABLE IF NOT EXISTS AGRI.PUBLIC.REFRESH_LOG (
  run_at TIMESTAMP_LTZ,
  summary STRING
);

CREATE EXTERNAL ACCESS INTEGRATION IF NOT EXISTS AGRI_APIS_INTEGRATION
  ALLOWED_NETWORK_RULES = (AGRI.PUBLIC.AGRI_APIS_NETWORK_RULE)
  ALLOWED_AUTHENTICATION_SECRETS = (AGRI.PUBLIC.DATA_GOV_KEY)
  ENABLED = TRUE;

CREATE OR REPLACE PROCEDURE AGRI.PUBLIC.REFRESH_LIVE_DATA()
  RETURNS STRING
  LANGUAGE PYTHON
  RUNTIME_VERSION = '3.11'
  PACKAGES = ('snowflake-snowpark-python', 'requests')
  HANDLER = 'main'
  EXTERNAL_ACCESS_INTEGRATIONS = (AGRI_APIS_INTEGRATION)
  SECRETS = ('data_gov_key' = AGRI.PUBLIC.DATA_GOV_KEY)
AS
$$
# Server-side twin of data/fetch_live_data.py — keep the two in sync.
import time
from datetime import date, datetime, timedelta, timezone

import _snowflake
import requests

DISTRICTS = {
    "Warangal": (17.978, 79.594),
    "Khammam": (17.247, 80.151),
    "Karimnagar": (18.438, 79.128),
    "Nizamabad": (18.673, 78.100),
}
IST = timezone(timedelta(hours=5, minutes=30))

# data.gov.in's WAF silently drops the default python-requests User-Agent
# (times out / 502s); any other UA is served instantly.
HEADERS = {"User-Agent": "kisan-copilot/1.0"}

WEATHER_MERGE = """
MERGE INTO AGRI.PUBLIC.WEATHER t
USING AGRI.PUBLIC.STG_WEATHER s
  ON t.district = s.DISTRICT AND t.date = TO_DATE(s.DAY)
WHEN MATCHED THEN UPDATE SET
  t.temp_max = s.TEMP_MAX, t.temp_min = s.TEMP_MIN,
  t.rainfall_mm = s.RAINFALL_MM, t.humidity = s.HUMIDITY
WHEN NOT MATCHED THEN INSERT (district, date, temp_max, temp_min, rainfall_mm, humidity)
  VALUES (s.DISTRICT, TO_DATE(s.DAY), s.TEMP_MAX, s.TEMP_MIN, s.RAINFALL_MM, s.HUMIDITY)
"""

MANDI_MERGE = """
MERGE INTO AGRI.PUBLIC.MANDI_PRICES t
USING AGRI.PUBLIC.STG_MANDI s
  ON t.commodity = s.COMMODITY AND t.variety = s.VARIETY AND t.state = s.STATE
 AND t.district = s.DISTRICT AND t.market = s.MARKET
 AND t.arrival_date = TO_DATE(s.ARRIVAL_DATE)
WHEN MATCHED THEN UPDATE SET
  t.min_price = s.MIN_PRICE, t.max_price = s.MAX_PRICE, t.modal_price = s.MODAL_PRICE
WHEN NOT MATCHED THEN INSERT
  (commodity, variety, state, district, market, min_price, max_price, modal_price, arrival_date)
  VALUES (s.COMMODITY, s.VARIETY, s.STATE, s.DISTRICT, s.MARKET,
          s.MIN_PRICE, s.MAX_PRICE, s.MODAL_PRICE, TO_DATE(s.ARRIVAL_DATE))
"""


def fetch_weather():
    today = datetime.now(IST).date()
    rows = []
    for district, (lat, lon) in DISTRICTS.items():
        resp = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat, "longitude": lon,
                "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum",
                "hourly": "relative_humidity_2m",
                "past_days": 10, "forecast_days": 1, "timezone": "Asia/Kolkata",
            },
            headers=HEADERS,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        humidity_by_day = {}
        for ts, rh in zip(data["hourly"]["time"], data["hourly"]["relative_humidity_2m"]):
            if rh is not None:
                humidity_by_day.setdefault(ts[:10], []).append(rh)
        daily = data["daily"]
        for i, day in enumerate(daily["time"]):
            if date.fromisoformat(day) > today:
                continue
            rh_vals = humidity_by_day.get(day)
            rows.append((
                district, day,
                daily["temperature_2m_max"][i], daily["temperature_2m_min"][i],
                daily["precipitation_sum"][i],
                round(sum(rh_vals) / len(rh_vals), 1) if rh_vals else None,
            ))
    return rows


def fetch_mandi(api_key):
    # Full national snapshot, deduped on the merge key: the feed splits
    # records by `grade`, which we don't store; duplicate source keys would
    # make the MERGE error out ("duplicate row detected").
    seen, rows, offset, limit = set(), [], 0, 1000
    while True:
        # data.gov.in is flaky under load (evening especially) — retry hard,
        # because the feed only carries today's snapshot: a failed run means
        # that day's data is gone for good.
        for attempt in range(5):
            try:
                resp = requests.get(
                    "https://api.data.gov.in/resource/9ef84268-d588-465a-a308-a864a43d0070",
                    params={
                        "api-key": api_key, "format": "json",
                        "limit": limit, "offset": offset,
                    },
                    headers=HEADERS,
                    timeout=90,
                )
                resp.raise_for_status()
                break
            except requests.RequestException:
                if attempt == 4:
                    raise
                time.sleep(5 * (2 ** attempt))
        records = resp.json().get("records", [])
        for r in records:
            try:
                arrival = datetime.strptime(r["arrival_date"], "%d/%m/%Y").date().isoformat()
                row = (
                    r["commodity"].strip(), (r.get("variety") or "Other").strip(),
                    r["state"].strip(), r["district"].strip(), r["market"].strip(),
                    float(r["min_price"]), float(r["max_price"]), float(r["modal_price"]),
                    arrival,
                )
            except (KeyError, ValueError, TypeError):
                continue
            key = (row[0], row[1], row[2], row[3], row[4], row[8])
            if key not in seen:  # first grade listed wins
                seen.add(key)
                rows.append(row)
        if len(records) < limit:
            return rows
        offset += limit


def fetch_mandi_safe(api_key):
    """Never leak the api key (it's in the URL) via exception text."""
    try:
        return fetch_mandi(api_key), None
    except requests.RequestException as e:
        status = getattr(getattr(e, "response", None), "status_code", "n/a")
        return None, f"{type(e).__name__} (http {status})"


def merge(session, rows, schema, stage_name, merge_sql):
    # Owner's-rights procs can't create TEMP tables; overwrite a small
    # permanent staging table instead (also handy for debugging runs).
    session.create_dataframe(rows, schema=schema).write.save_as_table(
        stage_name, mode="overwrite"
    )
    res = session.sql(merge_sql).collect()[0]
    return res[0], res[1]  # rows inserted, rows updated


def main(session):
    parts = []

    weather = fetch_weather()
    ins, upd = merge(
        session, weather,
        ["DISTRICT", "DAY", "TEMP_MAX", "TEMP_MIN", "RAINFALL_MM", "HUMIDITY"],
        "AGRI.PUBLIC.STG_WEATHER", WEATHER_MERGE,
    )
    parts.append(f"weather: {len(weather)} fetched, {ins} inserted, {upd} updated")

    api_key = _snowflake.get_generic_secret_string("data_gov_key")
    if api_key and api_key != "PENDING":
        # A mandi failure must not abort the weather load.
        mandi, err = fetch_mandi_safe(api_key)
        if err:
            parts.append(f"mandi: FAILED after retries: {err}")
        elif mandi:
            ins, upd = merge(
                session, mandi,
                ["COMMODITY", "VARIETY", "STATE", "DISTRICT", "MARKET",
                 "MIN_PRICE", "MAX_PRICE", "MODAL_PRICE", "ARRIVAL_DATE"],
                "AGRI.PUBLIC.STG_MANDI", MANDI_MERGE,
            )
            states = len({m[2] for m in mandi})
            parts.append(f"mandi: {len(mandi)} rows across {states} states, "
                         f"{ins} inserted, {upd} updated")
        else:
            parts.append("mandi: feed returned 0 rows")
    else:
        parts.append("mandi: SKIPPED (secret DATA_GOV_KEY still PENDING)")

    summary = "; ".join(parts)
    session.sql(
        "INSERT INTO AGRI.PUBLIC.REFRESH_LOG VALUES (CURRENT_TIMESTAMP(), ?)",
        params=[summary],
    ).collect()
    return summary
$$;

-- Task graph: refresh 13:30 + 18:30 IST, then prune >90-day-old prices.
-- Graph edits require the root suspended; child must go before the root
-- can be CREATE OR REPLACEd.
ALTER TASK IF EXISTS AGRI.PUBLIC.DAILY_REFRESH_TASK SUSPEND;
DROP TASK IF EXISTS AGRI.PUBLIC.PRUNE_MANDI_TASK;

-- Twice daily: 13:30 backstop (API reliably fast midday) + 18:30 full
-- evening snapshot. MERGE makes the double run duplicate-free.
CREATE OR REPLACE TASK AGRI.PUBLIC.DAILY_REFRESH_TASK
  WAREHOUSE = KISAN_WH
  SCHEDULE = 'USING CRON 30 13,18 * * * Asia/Kolkata'
  AS CALL AGRI.PUBLIC.REFRESH_LIVE_DATA();

-- ~6.5k national rows/day; 90 days keeps the 2-3 month trend window the
-- sell-or-hold advice needs while keeping scans tidy.
CREATE TASK AGRI.PUBLIC.PRUNE_MANDI_TASK
  WAREHOUSE = KISAN_WH
  AFTER AGRI.PUBLIC.DAILY_REFRESH_TASK
  AS DELETE FROM AGRI.PUBLIC.MANDI_PRICES
     WHERE arrival_date < DATEADD(day, -90, CURRENT_DATE());

ALTER TASK AGRI.PUBLIC.PRUNE_MANDI_TASK RESUME;
ALTER TASK AGRI.PUBLIC.DAILY_REFRESH_TASK RESUME;
