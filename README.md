# Twilio phone agent with AssemblyAI Universal-3.5 Pro Realtime

Build an AI phone agent that handles real calls using **Twilio Voice + Media Streams** and the **AssemblyAI Universal-3.5 Pro Realtime model** for real-time speech-to-text.

The key detail here: Twilio streams 8kHz μ-law (mulaw) audio. AssemblyAI Universal-3.5 Pro Realtime accepts `pcm_mulaw` at `sample_rate=8000` natively — no resampling, no format conversion.

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
AssemblyAI Universal-3.5      ElevenLabs TTS
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
- [AssemblyAI API key](https://app.assemblyai.com)
- [Twilio account](https://console.twilio.com) with a phone number
- [OpenAI API key](https://platform.openai.com/api-keys)
- [ElevenLabs API key](https://elevenlabs.io)
- [ngrok](https://ngrok.com) for local development

## Quick start

```bash
git clone https://github.com/kelsey-aai/voice-agent-twilio-universal-3-5-pro
cd voice-agent-twilio-universal-3-5-pro

python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Edit .env with your API keys

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
    "?speech_model=universal-3-5-pro"
    "&encoding=pcm_mulaw"      # must match Twilio's audio format
    "&sample_rate=8000"        # must match Twilio's 8kHz stream
    "&min_turn_silence=400"    # phone audio: wait a beat longer before ending the turn
    "&max_turn_silence=2000"   # hard ceiling so deliberate callers aren't cut off
)
```

Phone calls have more background noise than browser audio, so a slightly longer `min_turn_silence` reduces premature turn endings, while a `max_turn_silence` ceiling keeps deliberate callers from being cut off. Universal-3.5 Pro Realtime uses **punctuation-based** end-of-turn detection — `end_of_turn_confidence_threshold` does not apply to it (that parameter belongs to the older `universal-streaming` models).

## Extending the agent

### Add post-call transcription

```python
import assemblyai as aai
transcriber = aai.Transcriber()
transcript = transcriber.transcribe(recording_url)
print(transcript.text)
```

For full call analytics — speaker diarization, sentiment, action items — see [Tutorial 07: Retell + AssemblyAI](../07-retell-assemblyai), which uses the same AssemblyAI Audio Intelligence API pattern.

### Add keyterm prompting

```python
ASSEMBLYAI_WS_URL += "&keyterms_prompt=YourBrand&keyterms_prompt=SpecialTerm"
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

## Related tutorials

- [Tutorial 03: Vapi + AssemblyAI](../03-vapi-assemblyai) — managed voice platform that handles telephony for you
- [Tutorial 07: Retell + AssemblyAI](../07-retell-assemblyai) — post-call analytics with speaker diarization, sentiment, and LeMUR
- [Tutorial 05: raw WebSocket voice agent](../05-websocket-universal-3-pro) — the same AssemblyAI WebSocket pattern without Twilio

## Resources

- [AssemblyAI Universal Streaming docs](https://www.assemblyai.com/docs/speech-to-text/universal-streaming)
- [Twilio Media Streams docs](https://www.twilio.com/docs/voice/media-streams)
- [AssemblyAI Twilio tutorial](https://www.assemblyai.com/blog/transcribe-phone-call-real-time-python)

---

<div class="blog-cta_component">
  <div class="blog-cta_title">Build your Twilio phone agent today</div>
  <div class="blog-cta_rt w-richtext">
    <p>Sign up for a free AssemblyAI account and start transcribing Twilio calls with Universal-3.5 Pro Realtime in under 30 minutes.</p>
  </div>
  <a href="https://www.assemblyai.com/dashboard/signup" class="button w-button">Start building</a>
</div>

<div class="blog-cta_component">
  <div class="blog-cta_title">Scale multilingual phone transcription securely</div>
  <div class="blog-cta_rt w-richtext">
    <p>Discuss encryption, data residency, and regional compliance requirements. Our team can help plan integrations with platforms like Twilio for global rollouts.</p>
  </div>
  <a href="https://www.assemblyai.com/contact" class="button w-button">Talk to an AI expert</a>
</div>
