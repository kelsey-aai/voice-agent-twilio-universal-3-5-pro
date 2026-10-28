# Twilio phone agent with AssemblyAI Universal-3.6 Pro Realtime

Build an AI phone agent that handles real calls using **Twilio Voice + Media Streams** the **AssemblyAI Universal-3.6 Pro Realtime model** for real-time speech-to-text, OpenAI GPT-4o for replies, and **AssemblyAI Text-to-Speech** to speak them back into the call.

Companion repo for the blog post [Twilio phone agent with AssemblyAI Universal-3.6 Pro Realtime](https://www.assemblyai.com/blog/twilio-phone-agent-with-assemblyai).

The key detail here: Twilio streams 8kHz μ-law (mulaw) audio. Universal-3.6 Pro Realtime accepts `pcm_mulaw` at `sample_rate=8000` natively — no resampling, no format conversion. AssemblyAI Text-to-Speech closes the loop in the same format: the TTS session asks for `pcm_mulaw` at 8000 Hz, so the agent's replies go back to Twilio with no conversion on your side either.

## Architecture

```
Incoming call
     │
  Twilio Voice
     │ TwiML → open WebSocket
     ▼
Your server (/media-stream WebSocket)
     │                        │
     │ mulaw 8kHz audio       │ synthesized mulaw audio
     ▼                        ▲
AssemblyAI Universal-3.6      AssemblyAI TTS
Pro Realtime
(wss://streaming.assemblyai.com/v3/ws)
     │ transcript + turn signal
     ▼
  OpenAI GPT-4o
     │ text response
     └──────────────────────►
```

## Prerequisites

- Python 3.11+
- AssemblyAI API key ([free account](https://www.assemblyai.com/dashboard/signup)); one key covers speech-to-text and text-to-speech
- [Twilio account](https://console.twilio.com) with a phone number
- [OpenAI API key](https://platform.openai.com/api-keys)
- [ngrok](https://ngrok.com) for local development

## Quick start

```bash
git clone https://github.com/kelsey-aai/voice-agent-twilio-universal-3-5-pro
cd voice-agent-twilio-universal-3-5-pro

python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Edit .env: ASSEMBLYAI_API_KEY and OPENAI_API_KEY

# Start the server
uvicorn server:app --host 0.0.0.0 --port 8000

# Expose it publicly
ngrok http 8000
```

### Configure Twilio

1. Go to [Twilio Console](https://console.twilio.com) > Phone Numbers
2. Select your number > Voice & Fax
3. Set **A Call Comes In** to Webhook: `https://your-ngrok-url.ngrok.io/incoming-call`
4. Call your Twilio number

## AssemblyAI WebSocket parameters for Twilio

```python
ASSEMBLYAI_WS_URL = (
    "wss://streaming.assemblyai.com/v3/ws"
    "?speech_model=universal-3-6-pro"
    "&encoding=pcm_mulaw"      # must match Twilio's audio format
    "&sample_rate=8000"        # must match Twilio's 8kHz stream
    "&min_turn_silence=400"    # phone audio: wait a beat longer before ending the turn
    "&max_turn_silence=2000"   # hard ceiling so deliberate callers aren't cut off
)
```

The API key is sent in the `Authorization` header when the socket opens (see `server.py`).

Phone calls carry more background noise than browser audio, so a slightly longer `min_turn_silence` reduces premature turn endings. Universal-3.6 Pro Realtime uses end-of-turn detection that combines semantic context with voice activity, and entity-aware endpointing holds the turn open while a caller reads out a phone number, code, or email. You can also set the high-level mode preset (`min_latency`, `balanced`, `max_accuracy`) to shift the whole accuracy/latency balance at once.

Note: `end_of_turn_confidence_threshold` does not apply to Universal-3.6 Pro Realtime — that parameter belongs to the older `universal-streaming` models. Use `min_turn_silence` / `max_turn_silence` here.

**Heads up on model IDs:** this repo uses `universal-3-6-pro`, the current streaming default. `universal-3-5-pro` stays available if you need to pin the previous model; if you're still on the legacy `u3-rt-pro` ID, switch to `universal-3-6-pro`.

## Speak replies with AssemblyAI Text-to-Speech

`server.py` configures the TTS session for Twilio's 8 kHz μ-law: `encoding="pcm_mulaw"` and `sample_rate=8000`. The model's native output is 24 kHz 16-bit PCM, and the server resamples and encodes it before sending, so each chunk arrives raw, with no header, ready to base64-encode into a Twilio `media` event.

```python
import assemblyai as aai

# Twilio plays 8 kHz mu-law, so ask for exactly that: no transcoding on the way out
TTS_CONFIG = aai.TTSConfig(
    voice_id="49c443a4-8a1d-5f7e-b70b-98bdbd9823c3",  # Audrey
    language="english",
    encoding="pcm_mulaw",
    sample_rate=8000,
)


async def speak_on_call(ws: WebSocket, tts: aai.AsyncTTSSession, stream_sid: str, text: str):
    """Speak one reply on the call's TTS session and stream it into the Twilio call."""
    async for chunk in tts.speak(text):  # raw 8 kHz mu-law chunks
        await ws.send_text(json.dumps({
            "event": "media",
            "streamSid": stream_sid,
            "media": {"payload": base64.b64encode(chunk).decode()},
        }))
```

- **One session per call.** `media_stream` opens an `aai.AsyncTTSSession` when the call connects and reuses it for the greeting and every reply. A session opened per reply pays the connection handshake on every turn and spends your sessions-per-minute rate limit.
- **Keep-alive.** The session closes after two minutes without a frame, so `server.py` calls `await tts.keep_alive()` every 60 seconds; a session lasts at most an hour.
- **Barge-in (optional).** To let callers interrupt, call `await tts.cancel()` to stop the reply in flight and send Twilio `{"event": "clear", "streamSid": stream_sid}` to drop the audio it has already buffered.
- **Voices.** Change `voice_id` or `language` in `TTS_CONFIG`; see [Voices](https://www.assemblyai.com/docs/tts/voices) and [Optimizing accuracy and latency](https://www.assemblyai.com/docs/tts/getting-started/optimizing-accuracy-and-latency).

## Extending the agent

### Sharpen accuracy with conversation context

Universal-3.6 Pro Realtime can transcribe each caller turn in the context of what your agent just said — after your agent asks *"What's your account number?"*, the model is primed for the answer. `server.py` already does this: after each agent turn (and after the greeting) it pushes the agent's last reply mid-stream with an `UpdateConfiguration` message:

```python
await aai_ws.send(json.dumps({
    "type": "UpdateConfiguration",
    "agent_context": "Thanks for calling Acme. What's the account number on the policy?",
}))
```

### Handle noisy lines with Voice Focus

For calls from cars, speakerphones, or noisy rooms, add Voice Focus to isolate the caller's voice server-side:

```python
ASSEMBLYAI_WS_URL += "&voice_focus=far-field"
```

### Add keyterm prompting

```python
ASSEMBLYAI_WS_URL += "&keyterms_prompt=YourBrand&keyterms_prompt=SpecialTerm"
```

### Add post-call transcription

```python
import assemblyai as aai
transcriber = aai.Transcriber()
transcript = transcriber.transcribe(recording_url)
print(transcript.text)
```

## Deploy to Railway or Render

Both platforms support one-click Python web app deployment:

```bash
# Railway
railway login && railway init && railway up

# Render — create a Web Service pointing to this repo
# Build: pip install -r requirements.txt
# Start: uvicorn server:app --host 0.0.0.0 --port $PORT
```

Update your Twilio webhook to the production URL after deploying.

## Resources

- [Universal-3.6 Pro Realtime streaming API reference](https://www.assemblyai.com/docs/streaming/api-spec/streaming-websocket)
- [AssemblyAI streaming getting started](https://www.assemblyai.com/docs/streaming/getting-started/transcribe-streaming-audio)
- [AssemblyAI Text-to-Speech quickstart](https://www.assemblyai.com/docs/tts/getting-started/quickstart)
- [Twilio Media Streams docs](https://www.twilio.com/docs/voice/media-streams)

---

<div class="blog-cta_component">
  <div class="blog-cta_title">Launch your phone agent free</div>
  <div class="blog-cta_rt w-richtext">
    <p>Start free with Universal-3.6 Pro Realtime — $0.45/hr, keyterm prompting included, no minimums. Point Twilio at your server and go live in about half an hour.</p>
  </div>
  <a href="https://www.assemblyai.com/dashboard/signup" class="button w-button">Sign up free</a>
</div>

<div class="blog-cta_component">
  <div class="blog-cta_title">Scale multilingual phone transcription securely</div>
  <div class="blog-cta_rt w-richtext">
    <p>Discuss encryption, data residency, and regional compliance requirements. Our team can help plan integrations with platforms like Twilio for global rollouts.</p>
  </div>
  <a href="https://www.assemblyai.com/contact" class="button w-button">Talk to an AI expert</a>
</div>
