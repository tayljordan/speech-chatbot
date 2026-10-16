"""Voice bot: talk to it in English or Tamil and it answers in the same
language, out loud, in a voice you cloned.

    speech --> Gemini (understands + replies) --> ElevenLabs (speaks reply)
                         |
                 Firestore (conversations, per-turn metrics, daily usage)

Metric definitions are in METRICS.md.

Run with `.venv/bin/python app.py` and open http://localhost:8000. API keys
are read from .env (Secret Manager on Cloud Run) and never reach the browser.
"""
from __future__ import annotations

import hmac
import json
import os
import re
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, redirect, request, send_file, session
from google import genai
from google.cloud import firestore
from google.genai import types

from firestore_client import get_client

BASE = Path(__file__).parent
RECORDINGS = BASE / "recordings"
load_dotenv(BASE / ".env")

API = "https://api.elevenlabs.io"
HEADERS = {"xi-api-key": os.environ["ELEVENLABS_API_KEY"]}
# eleven_v3 is the model that speaks Tamil correctly in a cloned voice;
# multilingual_v2 produced gibberish for Tamil with the same voice.
TTS_MODEL = "eleven_v3"
VOICE_ID = re.compile(r"^[A-Za-z0-9]{1,64}$")
# Only your own voices are offered: stock and library voices are left out,
# and these clones are hidden (they stay in the ElevenLabs account).
MY_CATEGORIES = {"cloned", "generated"}
HIDDEN_VOICE_IDS = {"GRHT9e3YGxYLK1b6V2by"}  # Priya's voice
# Which cloned voice answers in which language, unless the page picks others.
DEFAULT_VOICES = {
    "en": "QhO1jtyR1D0inOZ7KkYa",  # Jordan
    "ta": "PcEfxb1KCLjbYCvnPIKy",  # Tamil voice (WhatsApp Audio 2026-10-16 combined.mp3)
}

GEMINI_MODEL = "gemini-3.5-flash-lite"
gemini = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
SYSTEM_PROMPT = """You are a friendly, helpful voice assistant for staff and students of AMET
University (Academy of Maritime Education and Training) in Chennai, India.

Tamil is the default language. Most users speak Tamil; some speak English.
- If the user's latest message is clearly in English, reply in English.
- Otherwise reply in Tamil, written in Tamil script. This includes Tamil, Tamil mixed
  with English words ("Tanglish", whether spoken or romanised), and anything unclear.

Your reply is read aloud, so keep it short and conversational (1-3 sentences unless they
ask for detail), with no markdown, lists, emoji or symbols that don't read well aloud.

Also return an exact transcript of the user's latest message in the script it was spoken
in (Tamil speech in Tamil script), and the language of your reply ("ta" or "en"). If the
audio is silent or unintelligible, return an empty transcript."""
TURN_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "transcript": {"type": "STRING"},
        "language": {"type": "STRING", "enum": ["en", "ta"]},
        "reply": {"type": "STRING"},
    },
    "required": ["transcript", "language", "reply"],
}

# Firestore: voice_bot_conversations/{id}/messages/{auto-id}
CONVERSATIONS = "voice_bot_conversations"
HISTORY_MESSAGES = 20  # how much earlier conversation Gemini sees
HISTORY_CHARS = 2000    # per message, to bound what the page can send
CONVERSATION_ID = re.compile(r"^[A-Za-z0-9]{1,64}$")

# Usage cap and metrics (see METRICS.md).
# voice_bot_usage/{YYYY-MM-DD in IST}: daily counters
# voice_bot_metrics/{reply message id}: one document per turn
USAGE = "voice_bot_usage"
METRICS = "voice_bot_metrics"
IST = ZoneInfo("Asia/Kolkata")
# ElevenLabs bills per character spoken. The Creator plan is 300,000 a month,
# so 10,000 a day keeps a month inside the plan.
DAILY_CHAR_LIMIT = int(os.environ.get("DAILY_CHAR_LIMIT", "10000"))
USAGE_CACHE_SECONDS = 30  # how stale the cap check may be (saves a Firestore read per turn)
CLIENT_METRICS = {"record_ms", "upload_bytes", "chat_roundtrip_ms", "audio_start_ms",
                  "end_to_end_ms", "client_wav_ms"}
background = ThreadPoolExecutor(max_workers=4)

APP_PASSWORD = os.environ["APP_PASSWORD"]

app = Flask(__name__)
app.secret_key = os.environ["SECRET_KEY"]
app.permanent_session_lifetime = timedelta(days=30)
ON_CLOUD_RUN = "K_SERVICE" in os.environ
app.config["SESSION_COOKIE_SECURE"] = ON_CLOUD_RUN


@app.before_request
def require_login():
    """One shared password guards the page and every API route."""
    if request.endpoint in ("login", "static") or session.get("authed"):
        return None
    if request.path.startswith("/api/"):
        return jsonify(error="Please log in again."), 401
    return redirect("/login")


@app.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    if request.method == "POST":
        if hmac.compare_digest(request.form.get("password", ""), APP_PASSWORD):
            session.permanent = True
            session["authed"] = True
            return redirect("/")
        time.sleep(1)  # slow down guessing
        error = "Wrong password."
    return send_file(BASE / "login.html") if not error else (
        (BASE / "login.html").read_text().replace("<!--error-->", error), 401
    )


@app.get("/logout")
def logout():
    session.clear()
    return redirect("/login")


def ms_since(start: float) -> int:
    return round((time.perf_counter() - start) * 1000)


def ist_day(offset: int = 0) -> str:
    return (datetime.now(IST) - timedelta(days=offset)).strftime("%Y-%m-%d")


_usage = {"day": None, "chars": 0, "read_at": 0.0}
_usage_lock = threading.Lock()


def chars_used_today() -> int:
    """Today's spoken characters, read from Firestore at most every 30 s."""
    day = ist_day()
    with _usage_lock:
        if _usage["day"] == day and time.monotonic() - _usage["read_at"] < USAGE_CACHE_SECONDS:
            return _usage["chars"]
    snap = get_client().collection(USAGE).document(day).get()
    chars = (snap.to_dict() or {}).get("tts_chars", 0) if snap.exists else 0
    with _usage_lock:
        _usage.update(day=day, chars=chars, read_at=time.monotonic())
    return chars


def add_usage(**counts: int) -> None:
    day = ist_day()
    get_client().collection(USAGE).document(day).set(
        {"date": day, **{k: firestore.Increment(v) for k, v in counts.items()}}, merge=True)
    with _usage_lock:
        if _usage["day"] == day:
            _usage["chars"] += counts.get("tts_chars", 0)


def limit_reached_response():
    return jsonify(error="Today's speaking limit has been reached. It resets at midnight (India time).",
                   limit=DAILY_CHAR_LIMIT), 429


def record_failure(stage: str, error: str, **extra) -> None:
    """Failed turns count in the stats too; written in the background."""
    doc = {"ts": datetime.now(timezone.utc), "ok": False, "stage": stage, "error": error[:300], **extra}
    background.submit(lambda: (get_client().collection(METRICS).add(doc),
                               add_usage(turns=1, errors=1)))


def relay_error(r: requests.Response):
    """Pass an ElevenLabs error back to the page as {"error": message}."""
    try:
        detail = r.json().get("detail", r.text)
    except ValueError:
        detail = r.text
    if isinstance(detail, dict):
        detail = detail.get("message", str(detail))
    return jsonify(error=str(detail)), r.status_code


@app.get("/")
def index():
    return send_file(BASE / "voice_studio.html")


@app.get("/api/voices")
def list_voices():
    r = requests.get(f"{API}/v1/voices", headers=HEADERS, timeout=30)
    if not r.ok:
        return relay_error(r)
    voices = [
        {"voice_id": v["voice_id"], "name": v["name"], "category": v.get("category")}
        for v in r.json()["voices"]
        if v.get("category") in MY_CATEGORIES and v["voice_id"] not in HIDDEN_VOICE_IDS
    ]
    return jsonify(voices=voices, defaults=DEFAULT_VOICES)


@app.post("/api/voices")
def create_voice():
    """Instant Voice Clone from one recorded sample."""
    name = request.form.get("name", "").strip()
    audio = request.files.get("audio")
    if not name or not audio:
        return jsonify(error="A voice name and a recording are required."), 400
    data = audio.read()
    r = requests.post(
        f"{API}/v1/voices/add",
        headers=HEADERS,
        data={"name": name},
        files={"files": ("sample.wav", data, "audio/wav")},
        timeout=120,
    )
    if not r.ok:
        return relay_error(r)
    voice_id = r.json()["voice_id"]
    # Keep the raw sample so the voice can be re-created without re-recording.
    # Skipped on Cloud Run, whose disk is wiped whenever the container restarts.
    if not ON_CLOUD_RUN:
        RECORDINGS.mkdir(exist_ok=True)
        (RECORDINGS / f"{voice_id}.wav").write_bytes(data)
    return jsonify(voice_id=voice_id, name=name)


@app.delete("/api/voices/<voice_id>")
def delete_voice(voice_id: str):
    """Permanently delete one of your own voices from ElevenLabs."""
    if not VOICE_ID.match(voice_id) or voice_id in HIDDEN_VOICE_IDS:
        return jsonify(error="That voice can't be deleted here."), 400
    r = requests.get(f"{API}/v1/voices/{voice_id}", headers=HEADERS, timeout=30)
    if not r.ok:
        return relay_error(r)
    if r.json().get("category") not in MY_CATEGORIES:
        return jsonify(error="Only your own voices can be deleted."), 400
    r = requests.delete(f"{API}/v1/voices/{voice_id}", headers=HEADERS, timeout=30)
    if not r.ok:
        return relay_error(r)
    (RECORDINGS / f"{voice_id}.wav").unlink(missing_ok=True)
    return jsonify(deleted=voice_id)


@app.post("/api/chat")
def chat():
    """One turn: user audio (or typed text) in, reply text out. The reply is
    spoken separately by reply_audio() so playback can start while it streams."""
    started = time.perf_counter()
    text = request.form.get("text", "").strip()
    audio = request.files.get("audio")
    if not text and not audio:
        return jsonify(error="Say or type something first."), 400
    usage_check = background.submit(chars_used_today)  # runs alongside Gemini

    # Firestore is in us-central1 and Cloud Run in asia-south1, so every round
    # trip is slow: the page sends the recent history it already shows instead
    # of the server reading it back, and the turn is saved in one batch below.
    try:
        sent = json.loads(request.form.get("history") or "[]")[-HISTORY_MESSAGES:]
    except ValueError:
        sent = []
    history = [
        types.Content(role="user" if m.get("role") == "user" else "model",
                      parts=[types.Part.from_text(text=str(m.get("text", ""))[:HISTORY_CHARS])])
        for m in sent if isinstance(m, dict) and m.get("text")
    ]
    audio_bytes = audio.read() if audio else b""
    said = (types.Part.from_bytes(data=audio_bytes, mime_type="audio/wav")
            if audio else types.Part.from_text(text=text))
    # 16 kHz, 16-bit mono WAV from the page: 32,000 bytes per second after the 44-byte header
    input_seconds = round(max(len(audio_bytes) - 44, 0) / 32000, 2) if audio else None
    gemini_started = time.perf_counter()
    try:
        result = gemini.models.generate_content(
            model=GEMINI_MODEL,
            contents=history + [types.Content(role="user", parts=[said])],
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                response_mime_type="application/json",
                response_schema=TURN_SCHEMA,
            ),
        )
        turn = json.loads(result.text)
    except Exception as e:  # network, quota, safety block, bad JSON
        record_failure("gemini", str(e), input_seconds=input_seconds)
        return jsonify(error=f"The language model didn't respond: {e}"), 502
    gemini_ms = ms_since(gemini_started)
    tokens = result.usage_metadata

    if usage_check.result() >= DAILY_CHAR_LIMIT:
        record_failure("limit", "daily character limit reached", input_seconds=input_seconds)
        return limit_reached_response()
    transcript = text or turn["transcript"].strip()
    if not transcript:
        record_failure("no_speech", "empty transcript", input_seconds=input_seconds, gemini_ms=gemini_ms)
        return jsonify(error="Sorry, I didn't catch that. Please try again."), 422
    reply, language = turn["reply"].strip(), turn["language"]
    voice_id = request.form.get(f"voice_{language}", "")
    if not VOICE_ID.match(voice_id):
        voice_id = DEFAULT_VOICES[language]

    conversations = get_client().collection(CONVERSATIONS)
    conversation_id = request.form.get("conversation_id", "")
    is_new = not CONVERSATION_ID.match(conversation_id)
    ref = conversations.document() if is_new else conversations.document(conversation_id)
    asked_at = datetime.now(timezone.utc)
    user_ref, reply_ref = ref.collection("messages").document(), ref.collection("messages").document()
    batch = get_client().batch()
    batch.set(ref, {"updated_at": firestore.SERVER_TIMESTAMP,
                    **({"created_at": firestore.SERVER_TIMESTAMP} if is_new else {})}, merge=True)
    batch.set(user_ref, {"role": "user", "text": transcript, "language": language,
                         "input": "text" if text else "voice", "created_at": asked_at})
    batch.set(reply_ref, {"role": "assistant", "text": reply, "language": language,
                          "voice_id": voice_id, "created_at": asked_at + timedelta(microseconds=1)})
    metrics = {
        "ts": asked_at, "ok": True, "conversation_id": ref.id, "language": language,
        "input": "text" if text else "voice", "input_seconds": input_seconds,
        "gemini_model": GEMINI_MODEL, "gemini_ms": gemini_ms,
        "prompt_tokens": getattr(tokens, "prompt_token_count", None),
        "output_tokens": getattr(tokens, "candidates_token_count", None),
        "history_messages": len(history), "reply_chars": len(reply),
        "pre_save_ms": ms_since(started),
    }
    batch.set(get_client().collection(METRICS).document(reply_ref.id), metrics)
    save_started = time.perf_counter()
    batch.commit()
    save_ms = ms_since(save_started)
    background.submit(add_usage, turns=1)

    return jsonify(
        conversation_id=ref.id,
        transcript=transcript,
        reply=reply,
        language=language,
        message_id=reply_ref.id,
        metrics={"gemini_ms": gemini_ms, "save_ms": save_ms, "server_ms": ms_since(started),
                 "input_seconds": input_seconds, "prompt_tokens": metrics["prompt_tokens"],
                 "output_tokens": metrics["output_tokens"], "reply_chars": len(reply)},
    )


@app.get("/api/conversations/<conversation_id>")
def get_conversation(conversation_id: str):
    """Messages of one conversation (text only), for redrawing after a reload."""
    if not CONVERSATION_ID.match(conversation_id):
        return jsonify(error="Unknown conversation."), 404
    ref = get_client().collection(CONVERSATIONS).document(conversation_id)
    if not ref.get().exists:
        return jsonify(error="Unknown conversation."), 404
    snaps = ref.collection("messages").order_by("created_at").limit_to_last(200).get()
    messages = [
        {"id": s.id, "role": s.get("role"), "text": s.get("text"), "language": s.to_dict().get("language")}
        for s in snaps
    ]
    return jsonify(conversation_id=conversation_id, messages=messages)


@app.get("/api/conversations/<conversation_id>/messages/<message_id>/audio")
def reply_audio(conversation_id: str, message_id: str):
    """Speak one assistant reply, streaming MP3 bytes as ElevenLabs makes them."""
    if not (CONVERSATION_ID.match(conversation_id) and CONVERSATION_ID.match(message_id)):
        return jsonify(error="Unknown message."), 404
    snap = (get_client().collection(CONVERSATIONS).document(conversation_id)
            .collection("messages").document(message_id).get())
    if not snap.exists or snap.get("role") != "assistant":
        return jsonify(error="Unknown message."), 404
    m = snap.to_dict()
    if chars_used_today() >= DAILY_CHAR_LIMIT:
        return limit_reached_response()
    tts_started = time.perf_counter()
    r = requests.post(
        f"{API}/v1/text-to-speech/{m['voice_id']}/stream",
        headers=HEADERS,
        params={"output_format": "mp3_44100_128"},
        json={"text": m["text"], "model_id": TTS_MODEL},
        stream=True,
        timeout=180,
    )
    if not r.ok:
        record_failure("tts", f"ElevenLabs {r.status_code}", message_id=message_id)
        return relay_error(r)

    def relay():
        first_byte_ms, size = None, 0
        for chunk in r.iter_content(chunk_size=4096):
            if first_byte_ms is None:
                first_byte_ms = ms_since(tts_started)
            size += len(chunk)
            yield chunk
        # After the last byte: record TTS timings and count the characters spoken.
        tts = {"tts_model": TTS_MODEL, "tts_first_byte_ms": first_byte_ms,
               "tts_total_ms": ms_since(tts_started), "tts_bytes": size,
               "reply_audio_seconds": round(size / 16000, 2)}  # 128 kbps MP3
        background.submit(lambda: (
            get_client().collection(METRICS).document(message_id).set(
                {"tts_plays": firestore.Increment(1), **tts}, merge=True),
            add_usage(tts_chars=len(m["text"]), tts_requests=1)))

    return Response(relay(), mimetype="audio/mpeg", headers={"Cache-Control": "no-store"})


@app.post("/api/metrics/<message_id>")
def client_metrics(message_id: str):
    """Timings only the browser can see (recording, upload, time until the voice starts)."""
    if not CONVERSATION_ID.match(message_id):
        return jsonify(error="Unknown message."), 404
    body = request.get_json(silent=True) or {}
    found = {k: round(float(v)) for k, v in body.items()
             if k in CLIENT_METRICS and isinstance(v, (int, float)) and 0 <= v < 10_000_000}
    if found:
        background.submit(lambda: get_client().collection(METRICS).document(message_id)
                          .set({"client": found}, merge=True))
    return jsonify(ok=True)


def summarise(values: list[float]) -> dict | None:
    values = sorted(v for v in values if isinstance(v, (int, float)))
    if not values:
        return None
    pick = lambda q: values[min(len(values) - 1, round(q * (len(values) - 1)))]
    return {"n": len(values), "mean": round(statistics.fmean(values)), "p50": pick(.5),
            "p90": pick(.9), "p95": pick(.95), "max": values[-1]}


@app.get("/api/stats")
def stats():
    """Aggregates for the Stats panel; raw JSON for anyone analysing the app."""
    days = min(max(int(request.args.get("days", 7)), 1), 90)
    since = datetime.now(timezone.utc) - timedelta(days=days)
    turns = [s.to_dict() for s in get_client().collection(METRICS)
             .where(filter=firestore.FieldFilter("ts", ">=", since)).limit(5000).stream()]
    ok = [t for t in turns if t.get("ok")]
    client = lambda key: [t.get("client", {}).get(key) for t in ok]
    usage_refs = [get_client().collection(USAGE).document(ist_day(i)) for i in range(days)]
    usage = [{"date": s.id, **(s.to_dict() or {})} for s in get_client().get_all(usage_refs) if s.exists]
    usage.sort(key=lambda u: u["date"], reverse=True)

    plan = None
    sub = requests.get(f"{API}/v1/user/subscription", headers=HEADERS, timeout=15)
    if sub.ok:
        d = sub.json()
        plan = {"tier": d.get("tier"), "characters_used": d.get("character_count"),
                "characters_limit": d.get("character_limit"),
                "resets_at": datetime.fromtimestamp(d["next_character_count_reset_unix"], timezone.utc).isoformat()
                if d.get("next_character_count_reset_unix") else None}

    failures: dict[str, int] = {}
    for t in turns:
        if not t.get("ok"):
            failures[t.get("stage", "unknown")] = failures.get(t.get("stage", "unknown"), 0) + 1
    today = next((u for u in usage if u["date"] == ist_day()), {})
    return jsonify(
        window_days=days,
        turns={"total": len(turns), "ok": len(ok), "failed": len(turns) - len(ok), "failures_by_stage": failures,
               "by_language": {lang: sum(1 for t in ok if t.get("language") == lang) for lang in ("ta", "en")}},
        latency_ms={
            "recording (user speaking)": summarise(client("record_ms")),
            "gemini (understand + reply)": summarise([t.get("gemini_ms") for t in ok]),
            "server total /api/chat": summarise([t.get("pre_save_ms") for t in ok]),
            "chat round trip (browser)": summarise(client("chat_roundtrip_ms")),
            "tts first byte (ElevenLabs)": summarise([t.get("tts_first_byte_ms") for t in ok]),
            "tts complete (ElevenLabs)": summarise([t.get("tts_total_ms") for t in ok]),
            "voice starts after reply text": summarise(client("audio_start_ms")),
            "end to end (stop talking -> hear voice)": summarise(client("end_to_end_ms")),
        },
        sizes={
            "input_seconds": summarise([t.get("input_seconds") for t in ok]),
            "reply_chars": summarise([t.get("reply_chars") for t in ok]),
            "reply_audio_seconds": summarise([t.get("reply_audio_seconds") for t in ok]),
            "prompt_tokens": summarise([t.get("prompt_tokens") for t in ok]),
            "output_tokens": summarise([t.get("output_tokens") for t in ok]),
        },
        today={"date": ist_day(), "tts_chars": today.get("tts_chars", 0), "daily_char_limit": DAILY_CHAR_LIMIT,
               "turns": today.get("turns", 0), "tts_requests": today.get("tts_requests", 0),
               "errors": today.get("errors", 0)},
        daily_usage=usage,
        elevenlabs_plan=plan,
        models={"gemini": GEMINI_MODEL, "tts": TTS_MODEL},
    )


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8000)
