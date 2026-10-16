# Metrics

How the voice bot is measured: what each number means, where it is taken, and
where it is stored. Everything here is visible in the app under **Stats**, and
as JSON at `GET /api/stats?days=N` (N = 1–90, login required).

## Pipeline

One "turn" is one question and its spoken answer:

```
 browser                         server (Flask, app.py)                external
 ───────                         ──────────────────────                ────────
 tap ─ record ─ tap
   │  record_ms
   ├─ WAV encode (16 kHz mono)
   │  client_wav_ms
   └─ POST /api/chat ──────────▶ parse + history ──────────────────▶ Gemini (audio in,
         chat_roundtrip_ms        gemini_ms ◀───────────────────────  JSON out: transcript,
                                  cap check (parallel)                language, reply)
                                  one Firestore batch write ─────────▶ Firestore (us-central1)
                                  server_ms (pre_save_ms + save_ms)
   ◀──── reply text + message_id
   GET /api/.../audio ─────────▶ stream ──────────────────────────────▶ ElevenLabs TTS
                                  tts_first_byte_ms, tts_total_ms      (eleven_v3, streaming)
   playback starts after ~2 s buffered
   audio_start_ms  (reply text received → voice audible)
   end_to_end_ms   (user tapped "done" → voice audible)
```

## Per-turn metrics

Stored in Firestore `voice_bot_metrics/{message_id}`. The document id is the
id of the assistant message in `voice_bot_conversations/{conversation_id}/messages`.

| Field | Unit | Measured by | Meaning |
|---|---|---|---|
| `ts` | UTC timestamp | server | When the turn was received |
| `ok` | bool | server | `false` for failed turns (see *Failures*) |
| `language` | `ta` / `en` | Gemini | Language of the reply (Tamil is the default) |
| `input` | `voice` / `text` | server | How the question arrived |
| `input_seconds` | s | server | Length of the user's audio, from the WAV size |
| `history_messages` | count | server | Earlier messages sent to Gemini as context (max 20) |
| `gemini_model` | — | server | Model id used |
| `gemini_ms` | ms | server | Gemini call: speech understanding + transcript + reply in one request |
| `prompt_tokens`, `output_tokens` | tokens | Gemini `usage_metadata` | Tokens billed for that call (audio counts as prompt tokens) |
| `reply_chars` | chars | server | Reply length; ElevenLabs bills per character |
| `pre_save_ms` | ms | server | `/api/chat` time from request start to just before the Firestore write |
| `tts_model` | — | server | ElevenLabs model id |
| `tts_first_byte_ms` | ms | server | ElevenLabs request → first audio byte received |
| `tts_total_ms` | ms | server | ElevenLabs request → last audio byte |
| `tts_bytes`, `reply_audio_seconds` | bytes, s | server | Reply audio size; seconds = bytes / 16,000 (128 kbps MP3) |
| `tts_plays` | count | server | Times this reply was synthesised (replays after a page reload re-synthesise and cost credits again) |
| `client.record_ms` | ms | browser | How long the user was recording |
| `client.client_wav_ms` | ms | browser | Converting the recording to 16 kHz WAV |
| `client.upload_bytes` | bytes | browser | Size of the uploaded WAV |
| `client.chat_roundtrip_ms` | ms | browser | POST `/api/chat` → reply text received (includes network) |
| `client.audio_start_ms` | ms | browser | Reply text received → audio actually playing |
| `client.end_to_end_ms` | ms | browser | User tapped "done" → audio actually playing. **The latency users feel.** |

`server_ms` and `save_ms` (Firestore batch commit) are returned to the browser
with each reply and shown in the Output box, but not stored.

Browser metrics are sent with `POST /api/metrics/{message_id}` once the reply
starts playing. If autoplay is blocked or the user leaves first, they are missing
for that turn, so `n` differs between rows in the latency table.

### Failures

Failed turns are stored with `ok: false`, a `stage` and an `error`:

| `stage` | Meaning |
|---|---|
| `gemini` | Gemini request failed (network, quota, safety block, invalid JSON) |
| `no_speech` | Gemini returned an empty transcript (silence or unintelligible audio) |
| `limit` | The daily character cap was reached |
| `tts` | ElevenLabs returned an error |

## Daily usage and the cap

Stored in `voice_bot_usage/{YYYY-MM-DD}` (date in India time, IST):

| Field | Meaning |
|---|---|
| `turns` | Turns handled (successful and failed) |
| `errors` | Failed turns |
| `tts_requests` | Audio syntheses (first plays + replays) |
| `tts_chars` | Characters sent to ElevenLabs, i.e. credits spent |

**Cap:** once `tts_chars` for the day reaches `DAILY_CHAR_LIMIT` (environment
variable, default **10,000**), `/api/chat` and the audio endpoint return HTTP 429
until midnight IST. The default keeps a month inside the ElevenLabs Creator plan
(300,000 characters/month).

The check reads Firestore at most every 30 s per server instance (cached in
memory) and runs in parallel with the Gemini call, so it adds no latency. With up
to 2 Cloud Run instances, the cap can be overshot by roughly one reply per instance.

## Stats summaries

`/api/stats` reports, for each latency and size metric, `n`, `p50`, `p90`, `p95`,
`max` and `mean`. Percentiles use nearest rank over successful turns in the
window (up to 5,000 turns). It also returns:

- turn counts: total, ok, failed, failures by stage, turns by language
- today's usage against the cap, plus per-day usage for the window
- the ElevenLabs plan balance from `GET /v1/user/subscription` (characters used,
  limit, reset date)

## Known caveats

- **Region split:** Cloud Run is in `asia-south1` (Mumbai) but Firestore is in
  `us-central1` (Iowa), so every Firestore round trip costs about 0.3–1.3 s. This is
  why each turn does one batched write and the browser sends the conversation
  history instead of the server reading it back. `save_ms` shows the cost.
- **Local vs. cloud:** when run locally, network distance to Gemini, ElevenLabs and
  Firestore differs from production, so compare timings within one environment only.
- **Clock sources:** server times use `time.perf_counter()`; browser times use
  `performance.now()`. They are never subtracted from each other.
