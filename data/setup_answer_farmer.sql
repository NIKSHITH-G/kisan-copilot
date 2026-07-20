-- ANSWER_FARMER: the app-path twin of the crop-advisory CoCo skill.
-- All reasoning stays inside Snowflake: this proc gathers weather (live),
-- crop stage, tiered fuzzy prices, MSP and Cortex Search agronomy
-- DETERMINISTICALLY, then has Cortex COMPLETE (llama3.3-70b) compose the
-- spoken sentences from ONLY that gathered data. The backend is pure glue
-- (Sarvam STT in, this CALL, Sarvam TTS out).
-- Deploy with: .venv/bin/python data/deploy_daily_refresh.py data/setup_answer_farmer.sql
--
-- CALL AGRI.PUBLIC.ANSWER_FARMER('Warangal','cotton',
--   'No rain for days, should I water? Should I sell?','Telugu');

USE ROLE ACCOUNTADMIN;
USE WAREHOUSE KISAN_WH;
USE SCHEMA AGRI.PUBLIC;

CREATE OR REPLACE PROCEDURE AGRI.PUBLIC.ANSWER_FARMER(
  DISTRICT_NAME STRING, CROP_NAME STRING, QUESTION STRING, LANGUAGE STRING)
  RETURNS VARIANT
  LANGUAGE PYTHON
  RUNTIME_VERSION = '3.11'
  PACKAGES = ('snowflake-snowpark-python')
  HANDLER = 'main'
AS
$$
import json

MODEL = "llama3.3-70b"

# farmer word -> ILIKE patterns (mirrors coco/crop_advisory/SKILL.md)
SYNONYMS = [
    (("cotton", "kapas", "patti"), ["%cotton%"]),
    (("paddy", "rice", "dhan", "vari"), ["%paddy%", "%rice%"]),
    (("chilli", "chili", "mirchi"), ["%chilli%", "%chili%"]),
    (("maize", "makka", "corn"), ["%maize%", "%corn%"]),
    (("onion", "pyaz", "ulli"), ["%onion%"]),
    (("tomato", "tamatar"), ["%tomato%"]),
    (("potato", "aloo", "batata"), ["%potato%"]),
    (("groundnut", "peanut", "moongphali"), ["%groundnut%", "%ground nut%"]),
    (("red gram", "redgram", "tur", "arhar", "kandi", "pigeon"),
     ["%arhar%", "%tur%", "%red gram%", "%pigeon%"]),
    (("soybean", "soya", "soy"), ["%soy%"]),
    (("wheat", "gehu", "godhuma"), ["%wheat%"]),
]


def patterns_for(crop):
    c = (crop or "").lower()
    for words, pats in SYNONYMS:
        if any(w in c for w in words):
            return pats
    return [f"%{c.strip()}%"] if c.strip() else ["%"]


def rows_to_dicts(rows, cols):
    return [dict(zip(cols, [str(v) if v is not None else None for v in r])) for r in rows]


def price_query(session, pats, extra_where, binds):
    ors = " OR ".join(["commodity ILIKE ?"] * len(pats))
    sql = f"""SELECT market, district, state, commodity, variety, modal_price,
                     TO_CHAR(arrival_date) arrival_date
              FROM AGRI.PUBLIC.MANDI_PRICES
              WHERE ({ors}) {extra_where}
                AND arrival_date >= CURRENT_DATE - 14
              ORDER BY arrival_date DESC, modal_price DESC LIMIT 50"""
    res = session.sql(sql, params=pats + binds).collect()
    return rows_to_dicts(res, ["market", "district", "state", "commodity",
                               "variety", "modal_price", "arrival_date"])


# Deterministic crop spotting (incl. native scripts) — runs BEFORE the LLM
# extraction because the crop list is closed and the LLM can misread scripts.
CROP_WORDS = [
    ("cotton", ["cotton", "kapas", "పత్తి", "कपास"]),
    ("paddy", ["paddy", "rice", "dhan", "వరి", "ధాన్యం", "धान", "चावल"]),
    ("chilli", ["chilli", "chili", "mirchi", "మిర్చి", "మిరప", "मिर्च"]),
    ("maize", ["maize", "corn", "makka", "మొక్కజొన్న", "मक्का"]),
    ("onion", ["onion", "pyaz", "ఉల్లి", "प्याज"]),
    ("tomato", ["tomato", "టమాట", "टमाटर"]),
    ("potato", ["potato", "aloo", "బంగాళదుంప", "आलू"]),
    ("groundnut", ["groundnut", "peanut", "వేరుశనగ", "मूंगफली"]),
    ("red gram", ["red gram", "tur dal", "arhar", "కంది", "अरहर"]),
    ("soybean", ["soybean", "soya", "సోయా", "सोयाबीन"]),
    ("wheat", ["wheat", "gehu", "గోధుమ", "गेहूं", "गेहूँ"]),
    ("apple", ["apple", "యాపిల్", "सेब"]),
]


def detect_crop_words(question):
    low = question.lower()
    for crop_name, words in CROP_WORDS:
        if any(w in low for w in words):
            return crop_name
    return ""


def extract_entities(session, question):
    """One COMPLETE call pulls district/crop/language out of the raw message
    (any Indian language/script) — no UI field needed."""
    prompt = ("Extract from this farmer message, which may be in Telugu, Hindi, "
              "English or any Indian language: (1) the Indian district or city "
              "mentioned, English spelling, empty string if none is mentioned; "
              "(2) the crop or commodity mentioned, common English name, empty "
              "if none; (3) the language of the message as one English word "
              "(Telugu, Hindi, English, Tamil, ...).\n"
              "Message: " + question + "\n"
              'Respond with STRICT JSON only: {"district": "", "crop": "", "language": ""}')
    raw = session.sql("SELECT SNOWFLAKE.CORTEX.COMPLETE(?, ?)",
                      params=[MODEL, prompt]).collect()[0][0].strip()
    if raw.startswith("```"):
        raw = raw[raw.find("{"):raw.rfind("}") + 1]
    try:
        d = json.loads(raw)
        return (str(d.get("district") or "").strip().title(),
                str(d.get("crop") or "").strip(),
                str(d.get("language") or "").strip().title())
    except Exception:
        return "", "", ""


def main(session, district_name, crop_name, question, language):
    district = (district_name or "").strip().title()
    crop = (crop_name or "").strip()
    language = (language or "").strip().title()

    # Detect whatever the caller did not supply, from the message itself.
    crop = crop or detect_crop_words(question)
    if not district or not crop or language in ("", "Auto"):
        ex_district, ex_crop, ex_language = extract_entities(session, question)
        district = district or ex_district
        crop = crop or ex_crop
        if language in ("", "Auto"):
            language = ex_language or "English"
    fallback_note = None
    if not district:
        district, fallback_note = "Warangal", "no district mentioned - using default"
    pats = patterns_for(crop)

    # 1. Live weather (proc geocodes + caches + merges history).
    weather = json.loads(session.sql(
        "CALL AGRI.PUBLIC.GET_DISTRICT_WEATHER(?)", params=[district]).collect()[0][0])
    if "error" in weather and district != "Warangal":
        # Unrecognized place name from extraction — fall back rather than fail.
        fallback_note = f"could not locate '{district}' - using Warangal"
        district = "Warangal"
        weather = json.loads(session.sql(
            "CALL AGRI.PUBLIC.GET_DISTRICT_WEATHER(?)", params=[district]).collect()[0][0])
    if "error" in weather:
        return {"error": weather["error"], "district": district}
    state = (weather.get("resolved_as") or "").split(",")[-1].strip()

    # 2. Crop stage (month-wrap aware).
    stage = rows_to_dicts(session.sql(
        """SELECT stage, water_need, notes FROM AGRI.PUBLIC.CROP_CALENDAR
           WHERE crop ILIKE ?
             AND ((stage_start_month <= stage_end_month
                   AND MONTH(CURRENT_DATE) BETWEEN stage_start_month AND stage_end_month)
               OR (stage_start_month > stage_end_month
                   AND (MONTH(CURRENT_DATE) >= stage_start_month
                        OR MONTH(CURRENT_DATE) <= stage_end_month)))""",
        params=[f"%{crop}%"]).collect(), ["stage", "water_need", "notes"])

    # 3. Prices: district -> state -> all-India. Never invent; report the tier.
    tier, prices = "district", price_query(
        session, pats, "AND district ILIKE ?", [f"%{district}%"])
    if not prices and state:
        tier, prices = "state", price_query(
            session, pats, "AND state ILIKE ?", [f"%{state}%"])
    if not prices:
        tier, prices = "all_india", price_query(session, pats, "", [])

    last_seen_local = None
    if tier != "district":
        ors = " OR ".join(["commodity ILIKE ?"] * len(pats))
        r = session.sql(
            f"SELECT TO_CHAR(MAX(arrival_date)) FROM AGRI.PUBLIC.MANDI_PRICES "
            f"WHERE ({ors}) AND district ILIKE ?",
            params=pats + [f"%{district}%"]).collect()
        last_seen_local = r[0][0]

    trend = None
    if prices:
        latest_date = prices[0]["arrival_date"]
        latest = [float(p["modal_price"]) for p in prices if p["arrival_date"] == latest_date]
        prior = [float(p["modal_price"]) for p in prices if p["arrival_date"] != latest_date]
        if latest and prior:
            trend = {"latest_avg": round(sum(latest) / len(latest)),
                     "prior_avg": round(sum(prior) / len(prior)),
                     "latest_date": latest_date}

    # 4. MSP — the ONLY permitted source of MSP figures.
    ors = " OR ".join(["commodity ILIKE ?"] * len(pats))
    msp = rows_to_dicts(session.sql(
        f"""SELECT commodity, variety_note, marketing_year, msp_per_quintal
            FROM AGRI.PUBLIC.MSP WHERE {ors}""", params=pats).collect(),
        ["commodity", "variety_note", "marketing_year", "msp_per_quintal"])

    # 5. Agronomy via Cortex Search.
    spec = json.dumps({"query": f"{crop} {question}"[:250],
                       "columns": ["crop", "topic", "content", "source"], "limit": 3})
    hits = json.loads(session.sql(
        "SELECT SNOWFLAKE.CORTEX.SEARCH_PREVIEW('AGRI.PUBLIC.ADVISORY_SEARCH', ?)",
        params=[spec]).collect()[0][0]).get("results", [])
    agronomy = [{"crop": h.get("crop"), "topic": h.get("topic"),
                 "content": h.get("content"), "source": h.get("source")} for h in hits]

    # Deterministic decisions — the LLM phrases these, it does not re-decide.
    hints = {}
    rain7 = float(weather.get("rain_last_7_days_mm") or 0)
    fc = weather.get("forecast_7_days") or []
    rain_next2 = round(sum(float(d.get("rain_mm") or 0) for d in fc[:2]), 1)
    need_high = any((s.get("water_need") or "").lower() == "high" for s in stage)
    if rain7 >= 20 or rain_next2 >= 10:
        hints["irrigation"] = (f"WET: {rain7}mm fell last week and {rain_next2}mm is "
                               f"forecast in 2 days - advise DO NOT irrigate now; drain excess water")
    elif rain7 < 5 and need_high:
        hints["irrigation"] = (f"DRY in high-need stage: only {rain7}mm last week - "
                               f"advise IRRIGATE now, light watering, no standing water")
    else:
        hints["irrigation"] = (f"MODERATE: {rain7}mm last week, {rain_next2}mm forecast - "
                               f"advise irrigate only if soil is dry at root depth")

    if tier != "district":
        top = prices[0] if prices else None
        hints["sell"] = (f"NO LOCAL DATA (tier={tier}, last seen in {district}: "
                         f"{last_seen_local or 'never'}). MUST say 'no current mandi data "
                         f"for {crop or 'this crop'} in {district}'"
                         + (f"; nearest reporting: {top['market']}, {top['state']}, "
                            f"Rs {top['modal_price']}/quintal on {top['arrival_date']} "
                            f"(clearly NOT local)" if top
                            else "; nothing reporting anywhere in the last 14 days"))
    elif trend:
        pct = round(100 * (trend["latest_avg"] - trend["prior_avg"]) / trend["prior_avg"], 1)
        word = "RISING" if pct > 1 else ("FALLING" if pct < -1 else "STEADY")
        hints["sell"] = (f"{word}: latest Rs {trend['latest_avg']} vs prior-week avg "
                         f"Rs {trend['prior_avg']} ({pct:+}%) - advise "
                         + ("HOLD or staggered selling" if pct > 1 else
                            ("sell sooner rather than later" if pct < -1 else
                             "sell as per cash need, no strong signal")))
        if msp:
            best_msp = max(float(m["msp_per_quintal"]) for m in msp)
            if trend["latest_avg"] < best_msp:
                hints["sell"] += (f"; market is BELOW MSP Rs {best_msp:.0f} - mention "
                                  f"government procurement as the floor option")

    hum = (weather.get("today") or {}).get("humidity")
    stage_notes = "; ".join(f"{s['stage']}: {s['notes']}" for s in stage)
    top_pest_chunk = next((a["content"] for a in agronomy
                           if (a.get("topic") or "") == "pests_diseases"), "")
    pest_source = f"{stage_notes} {top_pest_chunk}".lower()
    KNOWN_PESTS = ("pink bollworm", "fall armyworm", "stem borer",
                   "brown planthopper", "pod borer", "fruit borer", "leaf miner",
                   "girdle beetle", "late blight", "yellow rust", "purple blotch",
                   "thrips", "mites", "whitefly", "jassids", "aphids",
                   "sucking pests")
    named = [p for p in KNOWN_PESTS if p in pest_source][:2]
    action = ("set pheromone traps and check them daily" if any(
        "trap" in s for s in (top_pest_chunk.lower(), stage_notes.lower()))
        else "inspect leaf undersides on a few plants daily")
    hints["pest"] = (f"WATCH FOR: {', '.join(named) if named else 'seasonal pests'} "
                     f"(humidity {hum}%); ACTION: {action}. Phrase exactly this - "
                     f"never a generic 'spray pesticide'.")

    evidence = {
        "weather": {k: weather.get(k) for k in
                    ("district", "resolved_as", "rain_last_7_days_mm", "today",
                     "forecast_7_days")},
        "crop_stage": stage,
        "price_tier_used": tier,
        "price_rows": prices[:8],
        "price_trend": trend,
        "last_seen_in_district": last_seen_local,
        "msp": msp,
        "agronomy": agronomy,
        "computed_hints": hints,
    }
    if fallback_note:
        evidence["district_note"] = fallback_note

    prompt = f"""You are a trusted crop advisor speaking to an Indian farmer over voice.

FARMER: district={district}, crop={crop or 'not stated'}, question: {question}

DATA (the ONLY facts you may use — never add numbers from memory):
{json.dumps(evidence, ensure_ascii=False)}

Compose EXACTLY three one-sentence recommendations, in this order:
1. Irrigation — follow DATA.computed_hints.irrigation exactly; add the key
   numbers (rain, forecast) so the farmer hears why.
2. Sell or hold — follow DATA.computed_hints.sell exactly, quoting its
   figures with their market/date. Quote MSP only if present in DATA.msp.
3. Pest/crop watch — follow DATA.computed_hints.pest: name the specific
   pest/disease and the concrete check action. Call dosages indicative.

The hints are the decisions; your job is warm, clear phrasing — never
reverse or re-decide them, never add figures that are not in DATA.
Keep the three sentences short and speakable (under 90 words total).
Respond with STRICT JSON only, no markdown fences:
{{"spoken": "<the 3 sentences in {language}>", "english": "<English translation>"}}
If {language} is English, make both fields the same English text."""

    raw = session.sql("SELECT SNOWFLAKE.CORTEX.COMPLETE(?, ?)",
                      params=[MODEL, prompt]).collect()[0][0].strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw[raw.find("{"):raw.rfind("}") + 1]
    try:
        composed = json.loads(raw)
    except Exception:
        composed = {"spoken": raw, "english": raw}

    return {"district": district, "crop": crop, "language": language,
            "spoken": composed.get("spoken"), "english": composed.get("english"),
            "model": MODEL, "evidence": evidence}
$$;
