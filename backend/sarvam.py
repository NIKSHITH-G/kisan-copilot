"""Thin Sarvam AI client: STT (saarika), TTS (bulbul), translate (mayura).

Sarvam is the language layer only — all reasoning happens in Snowflake
(AGRI.PUBLIC.ANSWER_FARMER). Verified param styles 2026-07-19.
"""

import base64
import io
import os
import re
import wave

import requests

BASE = "https://api.sarvam.ai"
TTS_CHAR_LIMIT = 450  # bulbul rejects long inputs; chunk by sentence

# Sarvam language codes <-> names the Snowflake proc expects
LANG_NAMES = {
    "te-IN": "Telugu", "hi-IN": "Hindi", "en-IN": "English", "ta-IN": "Tamil",
    "kn-IN": "Kannada", "ml-IN": "Malayalam", "mr-IN": "Marathi",
    "bn-IN": "Bengali", "gu-IN": "Gujarati", "pa-IN": "Punjabi", "od-IN": "Odia",
}
NAME_CODES = {v: k for k, v in LANG_NAMES.items()}


def _headers():
    key = (os.environ.get("SARVAM_KEY") or "").strip() or None
    if not key:
        raise RuntimeError("SARVAM_KEY not set")
    return {"api-subscription-key": key}


def speech_to_text(audio_bytes: bytes, filename: str = "audio.wav"):
    """Returns (transcript, sarvam_language_code)."""
    r = requests.post(
        f"{BASE}/speech-to-text", headers=_headers(),
        files={"file": (filename, audio_bytes, "audio/wav")},
        data={"model": "saarika:v2.5"}, timeout=60,
    )
    r.raise_for_status()
    j = r.json()
    return j["transcript"], j.get("language_code") or "en-IN"


def translate(text: str, target_code: str, source_code: str = "en-IN") -> str:
    if target_code == source_code:
        return text
    r = requests.post(
        f"{BASE}/translate", headers=_headers(), timeout=60,
        json={"input": text, "source_language_code": source_code,
              "target_language_code": target_code},
    )
    r.raise_for_status()
    return r.json()["translated_text"]


def _tts_chunk(text: str, lang_code: str) -> bytes:
    r = requests.post(
        f"{BASE}/text-to-speech", headers=_headers(), timeout=120,
        json={"text": text, "target_language_code": lang_code,
              "speaker": "anushka", "model": "bulbul:v2"},
    )
    r.raise_for_status()
    return base64.b64decode(r.json()["audios"][0])


def text_to_speech(text: str, lang_code: str) -> bytes:
    """WAV bytes; sentence-chunks long text and concatenates the audio."""
    text = text.strip()
    if len(text) <= TTS_CHAR_LIMIT:
        return _tts_chunk(text, lang_code)

    chunks, current = [], ""
    for sentence in re.split(r"(?<=[.!?।])\s+", text):
        if len(current) + len(sentence) + 1 > TTS_CHAR_LIMIT and current:
            chunks.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        chunks.append(current)

    wavs = [_tts_chunk(c, lang_code) for c in chunks]
    out = io.BytesIO()
    with wave.open(io.BytesIO(wavs[0])) as first:
        params = first.getparams()
        frames = [first.readframes(first.getnframes())]
    for w in wavs[1:]:
        with wave.open(io.BytesIO(w)) as f:
            frames.append(f.readframes(f.getnframes()))
    with wave.open(out, "wb") as merged:
        merged.setparams(params)
        for fr in frames:
            merged.writeframes(fr)
    return out.getvalue()
