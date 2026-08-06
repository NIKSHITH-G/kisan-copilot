"""Kisan Copilot backend: voice in -> Snowflake reasoning -> voice out.

POST /ask
  multipart: audio=<wav/webm file> [+ district, crop]           (voice mode)
  or JSON:   {"text": "...", "district": "...", "crop": "...",
              "language_code": "te-IN"}                          (text mode)
  ->
  {"transcript", "language_code", "district", "crop",
   "spoken_text", "english", "audio_base64" (wav), "evidence"}

Run: .venv/bin/uvicorn backend.app:app --port 8400
"""

import base64
import json
import os
import sys
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent))
import actions
import sarvam
import snowflake_client

REPO_ROOT = Path(__file__).resolve().parent.parent

def load_env():
    env = REPO_ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                if v.strip() and k.strip() not in os.environ:
                    os.environ[k.strip()] = v.strip().strip("'\"")


load_env()
app = FastAPI(title="Kisan Copilot")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"])


class TextAsk(BaseModel):
    text: str
    district: str = ""       # optional — Snowflake extracts it from the message
    crop: str = ""
    language_code: str = ""  # optional — auto-detected when empty
    want_audio: bool = True  # set false to skip Sarvam TTS (e.g. bulk testing)


def _answer(transcript: str, language_code: str, district: str, crop: str,
            want_audio: bool = True) -> dict:
    # District/crop/language detection all happen inside ANSWER_FARMER.
    language_name = sarvam.LANG_NAMES.get(language_code, "Auto") if language_code else "Auto"

    t0 = _time.time()
    result = snowflake_client.answer_farmer(district, crop, transcript, language_name)
    t_proc = _time.time() - t0
    if "error" in result:
        raise HTTPException(status_code=422, detail=result["error"])
    district = result.get("district") or district
    crop = result.get("crop") or crop
    if not language_code:
        language_code = sarvam.NAME_CODES.get(result.get("language"), "en-IN")

    english = result.get("english") or result.get("spoken") or ""
    t0 = _time.time()
    if language_code.startswith("en"):
        spoken_text = english
    else:
        # Sarvam translate renders the final farmer-language text: better
        # Indic quality than the LLM's own attempt (kept as fallback).
        try:
            spoken_text = sarvam.translate(english, language_code)
        except Exception:
            spoken_text = result.get("spoken") or english
    t_translate = _time.time() - t0

    t0 = _time.time()
    audio_b64 = None
    if want_audio:
        audio_b64 = base64.b64encode(
            sarvam.text_to_speech(spoken_text, language_code)).decode()
    t_tts = _time.time() - t0

    evidence = result.get("evidence") or {}
    evidence["_backend_timings"] = {
        "answer_farmer_proc": round(t_proc, 2),
        "translate": round(t_translate, 2),
        "tts": round(t_tts, 2),
    }
    return {
        "transcript": transcript,
        "language_code": language_code,
        "district": district,
        "crop": crop,
        "spoken_text": spoken_text,
        "english": english,
        "audio_base64": audio_b64,
        "evidence": evidence,
    }


@app.post("/ask")
async def ask_voice(audio: UploadFile = File(...),
                    district: str = Form(""),
                    crop: str = Form("")):
    transcript, language_code = sarvam.speech_to_text(
        await audio.read(), audio.filename or "audio.wav")
    if not transcript.strip():
        raise HTTPException(status_code=422, detail="could not transcribe audio")
    return _answer(transcript, language_code, district, crop)


@app.post("/ask_text")
def ask_text(body: TextAsk):
    if not body.text.strip():
        raise HTTPException(status_code=422, detail="question text is empty")
    return _answer(body.text, body.language_code, body.district, body.crop,
                    want_audio=body.want_audio)


import time as _time

_cache: dict = {}


def _cached(key, ttl, fn):
    now = _time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    val = fn()
    _cache[key] = (now, val)
    return val


def _num(v):
    return float(v) if v is not None else None


@app.get("/prices")
def prices(q: str = "", state: str = "", commodity: str = "", limit: int = 25):
    """Live price browser: latest arrival per commodity+market (14-day window),
    with a real trend vs. that market's own prior records. All figures come
    straight from AGRI.PUBLIC.MANDI_PRICES — dates are true arrival dates."""
    limit = max(1, min(int(limit), 50))

    def run():
        where, params = ["arrival_date >= CURRENT_DATE - 14"], []
        if commodity:
            where.append("commodity ILIKE %s"); params.append(f"%{commodity}%")
        if state:
            where.append("state ILIKE %s"); params.append(f"%{state}%")
        if q:
            where.append("(commodity ILIKE %s OR market ILIKE %s OR district ILIKE %s OR state ILIKE %s)")
            params += [f"%{q}%"] * 4
        rows = snowflake_client.query(f"""
            SELECT commodity, variety, market, district, state,
                   MAX_BY(modal_price, arrival_date) AS latest_price,
                   TO_CHAR(MAX(arrival_date)) AS latest_date,
                   COUNT(*) AS n,
                   ARRAY_AGG(modal_price) WITHIN GROUP (ORDER BY arrival_date) AS series
            FROM AGRI.PUBLIC.MANDI_PRICES
            WHERE {' AND '.join(where)}
            GROUP BY 1,2,3,4,5
            ORDER BY MAX(arrival_date) DESC, latest_price DESC
            LIMIT {limit}""", params)
        import json as _json
        out = []
        for r in rows:
            series = [_num(x) for x in _json.loads(r["series"])] if isinstance(r["series"], str) else [_num(x) for x in (r["series"] or [])]
            latest = _num(r["latest_price"])
            prior = series[:-1]
            trend = (100 * (latest - sum(prior) / len(prior)) / (sum(prior) / len(prior))
                     if prior and latest is not None else None)
            out.append({"commodity": r["commodity"], "variety": r["variety"],
                        "market": r["market"], "district": r["district"], "state": r["state"],
                        "price": latest, "arrival_date": r["latest_date"],
                        "trend_pct": round(trend, 1) if trend is not None else None,
                        "series": series[-10:]})
        return out
    return _cached(("prices", q, state, commodity, limit), 120, run)


@app.get("/guide")
def guide(crop: str = "cotton"):
    """Crop guide straight from the curated corpus + crop calendar."""
    from datetime import datetime as _dt

    def run():
        chunks = snowflake_client.query(
            """SELECT topic, region, content, source FROM AGRI.PUBLIC.ADVISORY_CHUNKS
               WHERE crop ILIKE %s ORDER BY
               CASE topic WHEN 'sowing_seeds' THEN 1 WHEN 'fertilizer' THEN 2
                 WHEN 'irrigation' THEN 3 WHEN 'pests_diseases' THEN 4 ELSE 5 END""",
            (f"%{crop}%",))
        stages = snowflake_client.query(
            """SELECT stage, region, stage_start_month, stage_end_month, water_need, notes
               FROM AGRI.PUBLIC.CROP_CALENDAR WHERE crop ILIKE %s
               ORDER BY stage_start_month""", (f"%{crop}%",))
        month = _dt.now().month
        current_idx = None
        for i, s in enumerate(stages):
            a, b = int(s["stage_start_month"]), int(s["stage_end_month"])
            s["current"] = False
            if (a <= month <= b) if a <= b else (month >= a or month <= b):
                current_idx = i  # stages sharing a boundary month: the later stage wins
        if current_idx is not None:
            stages[current_idx]["current"] = True
        msp = snowflake_client.query(
            "SELECT variety_note, marketing_year, msp_per_quintal FROM AGRI.PUBLIC.MSP "
            "WHERE commodity ILIKE %s", (f"%{crop}%",))
        return {"crop": crop, "region": (chunks[0]["region"] if chunks else None),
                "chunks": chunks, "stages": stages, "msp": msp}
    return _cached(("guide", crop.lower()), 600, run)


@app.get("/snapshot")
def snapshot(district: str = "Warangal"):
    """Real numbers for Home + Field Mode: national ticker, district weather
    and arrivals, table counts, last pipeline run."""
    def run():
        ticker = snowflake_client.query("""
            SELECT commodity, market, state, modal_price, TO_CHAR(arrival_date) AS d
            FROM AGRI.PUBLIC.MANDI_PRICES
            WHERE arrival_date >= CURRENT_DATE - 3
              AND commodity ILIKE ANY ('tomato%','onion%','potato%','cotton%',
                                       'paddy%','wheat%','maize%','chilli%')
            QUALIFY ROW_NUMBER() OVER (PARTITION BY SPLIT_PART(commodity,'(',1)
                                       ORDER BY arrival_date DESC, modal_price DESC) = 1
            ORDER BY d DESC LIMIT 5""")
        # On-demand (cached) rather than reading WEATHER directly — a district
        # nobody has ever asked about has zero rows there, which silently
        # rendered as "0mm rain" instead of showing there's just no data yet.
        wx_row = snowflake_client.query(
            "CALL AGRI.PUBLIC.GET_DISTRICT_WEATHER(%s)", (district,))[0]
        wx = json.loads(list(wx_row.values())[0])
        today = wx.get("today") or {}
        weather = [{"d": today.get("date"), "temp_max": today.get("temp_max"),
                    "temp_min": today.get("temp_min"), "rainfall_mm": today.get("rain_mm"),
                    "humidity": today.get("humidity")}] if today else []
        rain_7d_live = wx.get("rain_last_7_days_mm")
        local = snowflake_client.query(
            """SELECT commodity, market, modal_price, TO_CHAR(arrival_date) AS d
               FROM AGRI.PUBLIC.MANDI_PRICES WHERE district ILIKE %s
               ORDER BY arrival_date DESC, modal_price DESC LIMIT 3""", (f"%{district}%",))
        # Combined into one round trip (was 2 separate queries) — each
        # Snowflake round trip has fixed dispatch overhead on top of the
        # query itself, and this endpoint's cold-start latency (~7s
        # uncached) was directly visible as the empty live-ticker pill
        # sitting there before it had anything to show.
        agg = snowflake_client.query(
            "SELECT s.n_rows, s.states, s.latest, l.run_at FROM "
            "(SELECT COUNT(*) AS n_rows, COUNT(DISTINCT state) AS states, "
            " TO_CHAR(MAX(arrival_date)) AS latest FROM AGRI.PUBLIC.MANDI_PRICES) s "
            "CROSS JOIN (SELECT TO_CHAR(MAX(run_at)) AS run_at FROM AGRI.PUBLIC.REFRESH_LOG) l")[0]
        return {"district": district,
                "ticker": [{**t, "modal_price": _num(t["modal_price"])} for t in ticker],
                "weather": [{**w, "rainfall_mm": _num(w["rainfall_mm"]),
                             "humidity": _num(w["humidity"]),
                             "temp_max": _num(w["temp_max"])} for w in weather],
                "rain_7d": _num(rain_7d_live) or 0,
                "local_prices": [{**p, "modal_price": _num(p["modal_price"])} for p in local],
                "stats": {"rows": int(agg["n_rows"]), "states": int(agg["states"]),
                          "latest_arrival": agg["latest"]},
                "last_refresh": agg["run_at"]}
    return _cached(("snapshot", district.lower()), 120, run)


# Same crop -> commodity match patterns as data/setup_answer_farmer.sql's
# SYNONYMS (kept in sync manually — the SQL side is the trust-critical path
# for /ask_text; this is a fully deterministic, no-LLM, glanceable summary).
_CROP_PATTERNS = [
    (("cotton", "kapas"), ["%cotton%"]),
    (("paddy", "rice", "dhan"), ["%paddy%", "%rice%"]),
    (("chilli", "chili"), ["%chilli%", "%chili%"]),
    (("maize", "corn"), ["%maize%", "%corn%"]),
    (("onion",), ["%onion%"]),
    (("tomato",), ["%tomato%"]),
    (("potato",), ["%potato%"]),
    (("groundnut", "peanut"), ["%groundnut%", "%ground nut%"]),
    (("red gram", "redgram", "tur", "arhar", "pigeon"), ["%arhar%", "%tur%", "%red gram%", "%pigeon%"]),
    (("soybean", "soya", "soy"), ["%soy%"]),
    (("wheat",), ["%wheat%"]),
]


def _patterns_for(crop: str):
    c = (crop or "").lower()
    for words, pats in _CROP_PATTERNS:
        if any(w in c for w in words):
            return pats
    return [f"%{c.strip()}%"] if c.strip() else ["%"]


def _price_query(pats, extra_where, binds):
    ors = " OR ".join(["commodity ILIKE %s"] * len(pats))
    sql = f"""SELECT market, district, state, commodity, variety, modal_price,
                     TO_CHAR(arrival_date) arrival_date
              FROM AGRI.PUBLIC.MANDI_PRICES
              WHERE ({ors}) {extra_where}
                AND arrival_date >= CURRENT_DATE - 14
              ORDER BY arrival_date DESC, modal_price DESC LIMIT 50"""
    return snowflake_client.query(sql, tuple(pats) + tuple(binds))


def _briefing_hints(weather, stage, tier, prices, trend, msp, crop, district):
    """Plain, direct sentences — this never goes through an LLM, so there
    are no internal labels to strip and no risk of the compose step
    leaking WET:/RISING:-style tags (the bug fixed earlier this session)."""
    rain7 = _num(weather.get("rain_last_7_days_mm")) or 0
    fc = weather.get("forecast_7_days") or []
    rain_next2 = round(sum(_num(d.get("rain_mm")) or 0 for d in fc[:2]), 1)
    need_high = any((s.get("water_need") or "").lower() == "high" for s in stage)
    if rain7 >= 20 or rain_next2 >= 10:
        irrigation = (f"Don't irrigate — {rain7}mm fell this week and {rain_next2}mm more "
                      f"is forecast in the next 2 days. Drain any standing water.")
    elif rain7 < 5 and need_high:
        irrigation = (f"Irrigate soon — only {rain7}mm fell this week and your crop is in "
                      f"a high water-need stage. Light watering, avoid standing water.")
    else:
        irrigation = f"{rain7}mm fell this week — irrigate only if the soil feels dry at root depth."

    if tier != "district" or not trend:
        if prices:
            top = prices[0]
            sell = (f"No mandi data for {crop} in {district} yet — nearest reporting: "
                    f"{top['market']}, {top['state']}, Rs {_num(top['modal_price']):.0f}/quintal "
                    f"on {top['arrival_date']} (not local).")
        else:
            sell = f"No mandi data reporting for {crop} anywhere in the last 14 days."
    else:
        pct = (round(100 * (trend["latest_avg"] - trend["prior_avg"]) / trend["prior_avg"], 1)
               if trend["prior_avg"] else 0)
        if pct > 1:
            sell = f"Prices are rising — Rs {trend['latest_avg']:.0f}, up {pct}% from last week. Hold or sell in stages."
        elif pct < -1:
            sell = f"Prices are falling — Rs {trend['latest_avg']:.0f}, down {abs(pct)}% from last week. Selling sooner may be better."
        else:
            sell = f"Prices are steady around Rs {trend['latest_avg']:.0f}. Sell as per your cash need."
        if msp:
            best_msp = max(_num(m["msp_per_quintal"]) for m in msp)
            if trend["latest_avg"] < best_msp:
                sell += f" Market is below MSP Rs {best_msp:.0f} — government procurement is a floor option."

    hum = (weather.get("today") or {}).get("humidity")
    pest = (f"Humidity is {hum}% — check leaf undersides on a few plants daily for early signs of pests."
            if hum is not None else "Check leaf undersides on a few plants daily for early signs of pests.")

    return {"irrigation": irrigation, "sell": sell, "pest": pest}


@app.get("/briefing")
def briefing(district: str = "Warangal", crop: str = "cotton"):
    """Proactive daily plan for Field Mode — the same deterministic
    thresholds ANSWER_FARMER uses, computed directly with no LLM call so
    it's instant and doesn't need the farmer to ask anything."""
    district = district or "Warangal"
    crop = crop or "cotton"

    def run():
        wx_row = snowflake_client.query(
            "CALL AGRI.PUBLIC.GET_DISTRICT_WEATHER(%s)", (district,))[0]
        weather = json.loads(list(wx_row.values())[0])
        resolved_district = district
        if "error" in weather:
            wx_row = snowflake_client.query(
                "CALL AGRI.PUBLIC.GET_DISTRICT_WEATHER(%s)", ("Warangal",))[0]
            weather = json.loads(list(wx_row.values())[0])
            resolved_district = "Warangal"
        state = (weather.get("resolved_as") or "").split(",")[-1].strip()

        stage = snowflake_client.query(
            """SELECT stage, water_need, notes FROM AGRI.PUBLIC.CROP_CALENDAR
               WHERE crop ILIKE %s
                 AND ((stage_start_month <= stage_end_month
                       AND MONTH(CURRENT_DATE) BETWEEN stage_start_month AND stage_end_month)
                   OR (stage_start_month > stage_end_month
                       AND (MONTH(CURRENT_DATE) >= stage_start_month
                            OR MONTH(CURRENT_DATE) <= stage_end_month)))""",
            (f"%{crop}%",))

        pats = _patterns_for(crop)
        tier, prices = "district", _price_query(pats, "AND district ILIKE %s", [f"%{resolved_district}%"])
        if not prices and state:
            tier, prices = "state", _price_query(pats, "AND state ILIKE %s", [f"%{state}%"])
        if not prices:
            tier, prices = "all_india", _price_query(pats, "", [])

        trend = None
        if prices:
            latest_date = prices[0]["arrival_date"]
            latest = [_num(p["modal_price"]) for p in prices if p["arrival_date"] == latest_date]
            prior = [_num(p["modal_price"]) for p in prices if p["arrival_date"] != latest_date]
            if latest and prior:
                trend = {"latest_avg": round(sum(latest) / len(latest)),
                         "prior_avg": round(sum(prior) / len(prior))}

        ors = " OR ".join(["commodity ILIKE %s"] * len(pats))
        msp = snowflake_client.query(
            f"SELECT commodity, variety_note, marketing_year, msp_per_quintal "
            f"FROM AGRI.PUBLIC.MSP WHERE {ors}", tuple(pats))

        hints = _briefing_hints(weather, stage, tier, prices, trend, msp, crop, resolved_district)

        return {"district": resolved_district, "crop": crop, "hints": hints,
                "price_tier_used": tier, "price_trend": trend,
                "price_rows": [{**p, "modal_price": _num(p["modal_price"])} for p in prices[:8]],
                "forecast_7_days": weather.get("forecast_7_days") or [],
                "crop_stage": stage[0] if stage else None}
    return _cached(("briefing", district.lower(), crop.lower()), 600, run)


class SendBody(BaseModel):
    to: str
    text: str
    channel: str = "whatsapp"


@app.post("/send")
def send(body: SendBody):
    """Send an advisory to a phone. Dry-runs until Twilio creds are in .env."""
    return actions.send_message(body.to, body.text, body.channel)


@app.get("/", include_in_schema=False)
def root():
    from fastapi.responses import FileResponse
    return FileResponse(REPO_ROOT / "frontend" / "index.html")


@app.get("/health")
def health():
    return {"snowflake": snowflake_client.ping(),
            "sarvam_key": bool(os.environ.get("SARVAM_KEY"))}
