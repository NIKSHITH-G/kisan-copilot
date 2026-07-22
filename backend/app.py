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

    result = snowflake_client.answer_farmer(district, crop, transcript, language_name)
    if "error" in result:
        raise HTTPException(status_code=422, detail=result["error"])
    district = result.get("district") or district
    crop = result.get("crop") or crop
    if not language_code:
        language_code = sarvam.NAME_CODES.get(result.get("language"), "en-IN")

    english = result.get("english") or result.get("spoken") or ""
    if language_code.startswith("en"):
        spoken_text = english
    else:
        # Sarvam translate renders the final farmer-language text: better
        # Indic quality than the LLM's own attempt (kept as fallback).
        try:
            spoken_text = sarvam.translate(english, language_code)
        except Exception:
            spoken_text = result.get("spoken") or english

    audio_b64 = None
    if want_audio:
        audio_b64 = base64.b64encode(
            sarvam.text_to_speech(spoken_text, language_code)).decode()

    return {
        "transcript": transcript,
        "language_code": language_code,
        "district": district,
        "crop": crop,
        "spoken_text": spoken_text,
        "english": english,
        "audio_base64": audio_b64,
        "evidence": result.get("evidence"),
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
        weather = snowflake_client.query(
            """SELECT TO_CHAR(date) AS d, temp_max, temp_min, rainfall_mm, humidity
               FROM AGRI.PUBLIC.WEATHER WHERE district ILIKE %s
               ORDER BY date DESC LIMIT 7""", (f"%{district}%",))
        local = snowflake_client.query(
            """SELECT commodity, market, modal_price, TO_CHAR(arrival_date) AS d
               FROM AGRI.PUBLIC.MANDI_PRICES WHERE district ILIKE %s
               ORDER BY arrival_date DESC, modal_price DESC LIMIT 3""", (f"%{district}%",))
        stats = snowflake_client.query(
            "SELECT COUNT(*) AS n_rows, COUNT(DISTINCT state) AS states, "
            "TO_CHAR(MAX(arrival_date)) AS latest FROM AGRI.PUBLIC.MANDI_PRICES")[0]
        log = snowflake_client.query(
            "SELECT TO_CHAR(MAX(run_at)) AS run_at FROM AGRI.PUBLIC.REFRESH_LOG")
        return {"district": district,
                "ticker": [{**t, "modal_price": _num(t["modal_price"])} for t in ticker],
                "weather": [{**w, "rainfall_mm": _num(w["rainfall_mm"]),
                             "humidity": _num(w["humidity"]),
                             "temp_max": _num(w["temp_max"])} for w in weather],
                "rain_7d": round(sum(_num(w["rainfall_mm"]) or 0 for w in weather), 1),
                "local_prices": [{**p, "modal_price": _num(p["modal_price"])} for p in local],
                "stats": {"rows": int(stats["n_rows"]), "states": int(stats["states"]),
                          "latest_arrival": stats["latest"]},
                "last_refresh": (log[0]["run_at"] if log else None)}
    return _cached(("snapshot", district.lower()), 120, run)


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
