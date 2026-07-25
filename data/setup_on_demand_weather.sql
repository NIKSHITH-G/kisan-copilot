-- On-demand weather for ANY Indian district, fully inside Snowflake.
-- Deploy with: .venv/bin/python data/deploy_daily_refresh.py data/setup_on_demand_weather.sql
--
-- CALL AGRI.PUBLIC.GET_DISTRICT_WEATHER('Anantapur');
--   1. Resolves district -> lat/lon via Open-Meteo geocoding, cached in
--      AGRI.PUBLIC.DISTRICT_GEO so repeat lookups skip the API.
--   2. Fetches past 10 days observed + 7-day forecast for that point.
--   3. MERGEs the observed days into AGRI.PUBLIC.WEATHER (so plain SQL /
--      the crop_advisory skill sees them), returns a JSON summary with the
--      forecast (forecast days are NOT written to WEATHER — that table
--      holds observations only).
-- The 4 pilot districts stay pre-loaded daily by DAILY_REFRESH_TASK as the
-- fast path / demo fallback.

USE ROLE ACCOUNTADMIN;
USE WAREHOUSE KISAN_WH;
USE SCHEMA AGRI.PUBLIC;

-- Add the geocoding host to the existing egress rule.
ALTER NETWORK RULE AGRI.PUBLIC.AGRI_APIS_NETWORK_RULE SET
  VALUE_LIST = ('api.open-meteo.com', 'geocoding-api.open-meteo.com', 'api.data.gov.in');

CREATE TABLE IF NOT EXISTS AGRI.PUBLIC.DISTRICT_GEO (
  district STRING,        -- name as asked (lookup key, case-insensitive)
  latitude FLOAT,
  longitude FLOAT,
  resolved_name STRING,   -- what the geocoder matched
  state STRING,
  cached_at TIMESTAMP_NTZ
);

-- Full response cache (forecast included) so a burst of questions about the
-- same district doesn't re-hit Open-Meteo every time — this call alone was
-- measured at 6+ of ~16s total ANSWER_FARMER time before caching.
CREATE TABLE IF NOT EXISTS AGRI.PUBLIC.WEATHER_CACHE (
  district STRING,
  cached_at TIMESTAMP_NTZ,
  response VARIANT
);

CREATE OR REPLACE PROCEDURE AGRI.PUBLIC.GET_DISTRICT_WEATHER(DISTRICT_NAME STRING)
  RETURNS VARIANT
  LANGUAGE PYTHON
  RUNTIME_VERSION = '3.11'
  PACKAGES = ('snowflake-snowpark-python', 'requests')
  HANDLER = 'main'
  EXTERNAL_ACCESS_INTEGRATIONS = (AGRI_APIS_INTEGRATION)
AS
$$
import json
from datetime import date, datetime, timedelta, timezone

import requests

IST = timezone(timedelta(hours=5, minutes=30))
HEADERS = {"User-Agent": "kisan-copilot/1.0"}
CACHE_TTL_MINUTES = 60  # advisory weather doesn't need to be fresher than this


def geocode(session, name):
    """Return (lat, lon, resolved_name, state, from_cache) or None."""
    hit = session.sql(
        "SELECT latitude, longitude, resolved_name, state "
        "FROM AGRI.PUBLIC.DISTRICT_GEO WHERE UPPER(district) = UPPER(?) LIMIT 1",
        params=[name],
    ).collect()
    if hit:
        r = hit[0]
        return r[0], r[1], r[2], r[3], True

    resp = requests.get(
        "https://geocoding-api.open-meteo.com/v1/search",
        params={"name": name, "count": 10, "language": "en", "format": "json"},
        headers=HEADERS,
        timeout=30,
    )
    resp.raise_for_status()
    results = [r for r in resp.json().get("results", [])
               if r.get("country_code") == "IN"]
    if not results:
        return None
    best = results[0]
    lat, lon = best["latitude"], best["longitude"]
    resolved, state = best.get("name", name), best.get("admin1", "")
    session.sql(
        "INSERT INTO AGRI.PUBLIC.DISTRICT_GEO VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP())",
        params=[name, lat, lon, resolved, state],
    ).collect()
    return lat, lon, resolved, state, False


def main(session, district_name):
    name = (district_name or "").strip().title()
    if not name:
        return {"error": "empty district name"}

    hit = session.sql(
        "SELECT response FROM AGRI.PUBLIC.WEATHER_CACHE WHERE UPPER(district) = UPPER(?) "
        "AND cached_at > DATEADD('minute', ?, CURRENT_TIMESTAMP()) LIMIT 1",
        params=[name, -CACHE_TTL_MINUTES],
    ).collect()
    if hit:
        return json.loads(hit[0][0])

    geo = geocode(session, name)
    if geo is None:
        return {"error": f"could not geocode district '{name}' in India"}
    lat, lon, resolved, state, cached = geo

    resp = requests.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": lat, "longitude": lon,
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum",
            "hourly": "relative_humidity_2m",
            "past_days": 10, "forecast_days": 7, "timezone": "Asia/Kolkata",
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

    today = datetime.now(IST).date()
    observed, forecast = [], []
    daily = data["daily"]
    for i, day in enumerate(daily["time"]):
        rh_vals = humidity_by_day.get(day)
        row = (
            name, day,
            daily["temperature_2m_max"][i], daily["temperature_2m_min"][i],
            daily["precipitation_sum"][i],
            round(sum(rh_vals) / len(rh_vals), 1) if rh_vals else None,
        )
        if date.fromisoformat(day) <= today:
            observed.append(row)
        else:
            forecast.append({"date": day, "temp_max": row[2], "temp_min": row[3],
                             "rain_mm": row[4]})

    # MERGE observed days via an inline VALUES source (no staging table, so
    # concurrent calls for different districts can't trample each other).
    placeholders = ", ".join(["(?, ?, ?, ?, ?, ?)"] * len(observed))
    flat = [v for row in observed for v in row]
    session.sql(f"""
        MERGE INTO AGRI.PUBLIC.WEATHER t
        USING (SELECT column1 district, TO_DATE(column2) day, column3 tmax,
                      column4 tmin, column5 rain, column6 hum
               FROM VALUES {placeholders}) s
          ON t.district = s.district AND t.date = s.day
        WHEN MATCHED THEN UPDATE SET t.temp_max = s.tmax, t.temp_min = s.tmin,
          t.rainfall_mm = s.rain, t.humidity = s.hum
        WHEN NOT MATCHED THEN INSERT (district, date, temp_max, temp_min, rainfall_mm, humidity)
          VALUES (s.district, s.day, s.tmax, s.tmin, s.rain, s.hum)
    """, params=flat).collect()

    last7 = [r for r in observed if (today - date.fromisoformat(r[1])).days < 7]
    result = {
        "district": name,
        "resolved_as": f"{resolved}, {state}",
        "latitude": lat, "longitude": lon,
        "geocode_from_cache": cached,
        "observed_days_merged_into_WEATHER": len(observed),
        "rain_last_7_days_mm": round(sum(r[4] or 0 for r in last7), 1),
        "today": {"date": observed[-1][1], "temp_max": observed[-1][2],
                  "temp_min": observed[-1][3], "rain_mm": observed[-1][4],
                  "humidity": observed[-1][5]} if observed else None,
        "forecast_7_days": forecast,
    }

    session.sql(
        "MERGE INTO AGRI.PUBLIC.WEATHER_CACHE t USING (SELECT ? d, PARSE_JSON(?) r) s "
        "ON t.district = s.d "
        "WHEN MATCHED THEN UPDATE SET t.cached_at = CURRENT_TIMESTAMP(), t.response = s.r "
        "WHEN NOT MATCHED THEN INSERT (district, cached_at, response) "
        "VALUES (s.d, CURRENT_TIMESTAMP(), s.r)",
        params=[name, json.dumps(result)],
    ).collect()

    return result
$$;
