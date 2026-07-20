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

# Detect the crop from the transcript when the caller does not pass one.
# Detection only — advice logic lives entirely in Snowflake.
CROP_WORDS = {
    "cotton": ["cotton", "kapas", "పత్తి", "कपास"],
    "paddy": ["paddy", "rice", "dhan", "వరి", "ధాన్యం", "धान", "चावल"],
    "chilli": ["chilli", "chili", "mirchi", "మిర్చి", "మిరప", "मिर्च"],
    "maize": ["maize", "corn", "makka", "మొక్కజొన్న", "मक्का"],
    "onion": ["onion", "pyaz", "ఉల్లి", "प्याज"],
    "tomato": ["tomato", "టమాట", "टमाटर"],
    "potato": ["potato", "aloo", "బంగాళదుంప", "आलू"],
    "groundnut": ["groundnut", "peanut", "వేరుశనగ", "मूंगफली"],
    "red gram": ["red gram", "tur", "arhar", "కంది", "अरहर", "तूर"],
    "soybean": ["soybean", "soya", "సోయా", "सोयाबीन"],
    "wheat": ["wheat", "gehu", "గోధుమ", "गेहूं", "गेहूँ"],
    "apple": ["apple", "యాపిల్", "సేపు", "सेब"],
}


def detect_crop(text: str) -> str:
    low = (text or "").lower()
    for crop, words in CROP_WORDS.items():
        if any(w in low for w in words):
            return crop
    return ""


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
    district: str = "Warangal"
    crop: str = ""
    language_code: str = "en-IN"


def _answer(transcript: str, language_code: str, district: str, crop: str,
            want_audio: bool = True) -> dict:
    crop = crop or detect_crop(transcript)
    language_name = sarvam.LANG_NAMES.get(language_code, "English")

    result = snowflake_client.answer_farmer(district, crop, transcript, language_name)
    if "error" in result:
        raise HTTPException(status_code=422, detail=result["error"])

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
                    district: str = Form("Warangal"),
                    crop: str = Form("")):
    transcript, language_code = sarvam.speech_to_text(
        await audio.read(), audio.filename or "audio.wav")
    if not transcript.strip():
        raise HTTPException(status_code=422, detail="could not transcribe audio")
    return _answer(transcript, language_code, district, crop)


@app.post("/ask_text")
def ask_text(body: TextAsk):
    return _answer(body.text, body.language_code, body.district, body.crop)


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
