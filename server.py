"""
Twilio phone call voice agent with AssemblyAI Universal-3 Pro Streaming.

Architecture:
  Caller ──► Twilio ──► (Media Streams WebSocket) ──► This server
                                                           │
                                          AssemblyAI U3 Pro STT (mulaw 8kHz)
                                                           │ transcript
                                                      OpenAI GPT-4o
                                                           │ text
                                                      ElevenLabs TTS
                                                           │ mulaw audio
                                          ◄── injected back into Twilio call

Run with:
  uvicorn server:app --host 0.0.0.0 --port 8000

Expose via ngrok:
  ngrok http 8000

Set Twilio webhook:
  Voice & Fax > A Call Comes In > Webhook > https://<ngrok>/incoming-call
"""

import asyncio
import base64
import json
import os
from typing import Optional

import assemblyai as aai
import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from openai import AsyncOpenAI

load_dotenv()

app = FastAPI(title="Twilio + AssemblyAI U3 Pro Voice Agent")

# ── Clients ──────────────────────────────────────────────────────────────────
aai.settings.api_key = os.environ["ASSEMBLYAI_API_KEY"]
openai_client = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"])
ELEVENLABS_API_KEY = os.environ["ELEVENLABS_API_KEY"]
ELEVENLABS_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID", "EXAVITQu4vr4xnSDxMaL")

SYSTEM_PROMPT = """
You are a helpful phone voice assistant. Keep every response under 2 sentences.
Speak naturally — no markdown, no lists. You are on a phone call.
""".strip()

# Twilio streams audio as 8kHz mulaw — AssemblyAI U3 Pro accepts this natively.
ASSEMBLYAI_WS_URL = (
    "wss://streaming.assemblyai.com/v3/ws"
    "?speech_model=u3-rt-pro"
    "&encoding=pcm_mulaw"
    "&sample_rate=8000"
    "&end_of_turn_confidence_threshold=0.5"
    "&min_turn_silence=400"
    "&max_turn_silence=1200"
    f"&token={os.environ['ASSEMBLYAI_API_KEY']}"
)


# ── TwiML endpoint ────────────────────────────────────────────────────────────

@app.post("/incoming-call")
async def incoming_call(request: Request):
    """Return TwiML that opens a Media Streams WebSocket to this server."""
    host = request.headers.get("host", "your-ngrok-url.ngrok.io")
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say voice="Polly.Joanna">Hello! How can I help you today?</Say>
  <Connect>
    <Stream url="wss://{host}/media-stream" />
  </Connect>
</Response>"""
    return Response(content=twiml, media_type="application/xml")


# ── Media Streams WebSocket handler ──────────────────────────────────────────

@app.websocket("/media-stream")
async def media_stream(ws: WebSocket):
    """Bridge: Twilio audio ──► AssemblyAI U3 Pro ──► GPT-4o ──► ElevenLabs ──► Twilio."""
    await ws.accept()

    stream_sid: Optional[str] = None
    conversation: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]

    # ── Open AssemblyAI WebSocket ─────────────────────────────────────────
    import websockets

    async with websockets.connect(ASSEMBLYAI_WS_URL) as aai_ws:

        async def forward_audio_to_assemblyai():
            """Read audio from Twilio, forward raw mulaw bytes to AssemblyAI."""
            nonlocal stream_sid
            try:
                async for raw in ws.iter_text():
                    msg = json.loads(raw)
                    event = msg.get("event")

                    if event == "start":
                        stream_sid = msg["start"]["streamSid"]
                        print(f"Stream started: {stream_sid}")

                    elif event == "media":
                        # Twilio sends base64-encoded mulaw audio
                        audio_bytes = base64.b64decode(msg["media"]["payload"])
                        await aai_ws.send(audio_bytes)

                    elif event == "stop":
                        print("Stream stopped by Twilio")
                        # Gracefully terminate AssemblyAI session
                        await aai_ws.send(json.dumps({"type": "Terminate"}))
                        break
            except WebSocketDisconnect:
                pass

        async def receive_transcripts_and_respond():
            """Receive transcripts from AssemblyAI, generate + inject responses."""
            try:
                async for raw in aai_ws:
                    msg = json.loads(raw)
                    msg_type = msg.get("message_type") or msg.get("type", "")

                    if msg_type == "Begin":
                        print(f"AssemblyAI session: {msg.get('id')}")

                    elif msg_type == "Turn":
                        # Only act on final (end-of-turn) transcripts
                        if msg.get("end_of_turn") and msg.get("transcript", "").strip():
                            user_text = msg["transcript"].strip()
                            print(f"👤 User: {user_text}")

                            # Generate LLM response
                            conversation.append({"role": "user", "content": user_text})
                            reply = await generate_llm_response(conversation)
                            conversation.append({"role": "assistant", "content": reply})
                            print(f"🤖 Agent: {reply}")

                            # Synthesise and inject audio back into the call
                            if stream_sid:
                                await speak_on_call(ws, stream_sid, reply)

            except Exception as e:
                print(f"AssemblyAI receive error: {e}")

        # Run both coroutines concurrently
        await asyncio.gather(
            forward_audio_to_assemblyai(),
            receive_transcripts_and_respond(),
        )


async def generate_llm_response(messages: list[dict]) -> str:
    """Call OpenAI GPT-4o with the conversation history."""
    response = await openai_client.chat.completions.create(
        model="gpt-4o",
        messages=messages,
        max_tokens=150,
        temperature=0.7,
    )
    return response.choices[0].message.content.strip()


async def speak_on_call(ws: WebSocket, stream_sid: str, text: str):
    """
    Synthesise text with ElevenLabs (mulaw 8kHz) and inject into the Twilio call
    via the Media Streams 'media' event.
    """
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}/stream",
            headers={
                "xi-api-key": ELEVENLABS_API_KEY,
                "Content-Type": "application/json",
            },
            json={
                "text": text,
                "model_id": "eleven_turbo_v2",
                "output_format": "ulaw_8000",  # matches Twilio's required format
            },
            timeout=10,
        )
        if resp.status_code != 200:
            print(f"TTS error: {resp.status_code}")
            return

        audio_b64 = base64.b64encode(resp.content).decode()

    # Inject audio into the active Twilio call
    await ws.send_text(json.dumps({
        "event": "media",
        "streamSid": stream_sid,
        "media": {"payload": audio_b64},
    }))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
