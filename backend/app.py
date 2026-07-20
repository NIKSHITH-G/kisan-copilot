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
