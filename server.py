"""
Twilio phone call voice agent with AssemblyAI Universal-3.6 Pro Realtime.

Architecture:
  Caller ──► Twilio ──► (Media Streams WebSocket) ──► This server
                                                           │
                                          AssemblyAI Universal-3.6 Pro Realtime (mulaw 8kHz)
                                                           │ transcript
                                                      OpenAI GPT-4o
                                                           │ text
                                              AssemblyAI Text-to-Speech (pcm_mulaw 8kHz)
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
from dotenv import load_dotenv
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from openai import AsyncOpenAI

load_dotenv()

app = FastAPI(title="Twilio + AssemblyAI Universal-3.6 Pro Realtime Voice Agent")

# ── Clients ──────────────────────────────────────────────────────────────────
aai.settings.api_key = os.environ["ASSEMBLYAI_API_KEY"]
openai_client = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"])

# Twilio plays 8 kHz mu-law, so ask for exactly that: no transcoding on the way out
TTS_CONFIG = aai.TTSConfig(
    voice_id="49c443a4-8a1d-5f7e-b70b-98bdbd9823c3",  # Audrey
    language="english",
    encoding="pcm_mulaw",
    sample_rate=8000,
)

SYSTEM_PROMPT = """
You are a helpful phone voice assistant. Keep every response under 2 sentences.
Speak naturally — no markdown, no lists. You are on a phone call.
""".strip()

GREETING = "Hello! How can I help you today?"

# Twilio streams audio as 8kHz mulaw — Universal-3.6 Pro Realtime accepts this
# natively. The API key goes in the Authorization header (see media_stream);
# the `token` query param is only for temporary streaming tokens.
ASSEMBLYAI_WS_URL = (
    "wss://streaming.assemblyai.com/v3/ws"
    "?speech_model=universal-3-6-pro"
    "&encoding=pcm_mulaw"      # must match Twilio's audio format
    "&sample_rate=8000"        # must match Twilio's 8kHz stream
    "&min_turn_silence=400"    # phone audio: wait a beat longer before ending the turn
    "&max_turn_silence=2000"   # hard ceiling so deliberate callers aren't cut off
)


# ── TwiML endpoint ────────────────────────────────────────────────────────────

@app.post("/incoming-call")
async def incoming_call(request: Request):
    """Return TwiML that opens a Media Streams WebSocket to this server."""
    host = request.headers.get("host", "your-ngrok-url.ngrok.io")
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <Stream url="wss://{host}/media-stream" />
  </Connect>
</Response>"""
    return Response(content=twiml, media_type="application/xml")


# ── Media Streams WebSocket handler ──────────────────────────────────────────

@app.websocket("/media-stream")
async def media_stream(ws: WebSocket):
    """Bridge: Twilio audio ──► AssemblyAI Universal-3.6 Pro Realtime ──► GPT-4o ──► AssemblyAI TTS ──► Twilio."""
    await ws.accept()

    stream_sid: Optional[str] = None
    greeting_task: Optional[asyncio.Task] = None
    conversation: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]

    # ── Open AssemblyAI WebSockets ────────────────────────────────────────
    import websockets

    # One TTS session per call: opened when the call starts, reused for every reply
    async with aai.AsyncTTSSession(
        TTS_CONFIG, api_key=os.environ["ASSEMBLYAI_API_KEY"]
    ) as tts, websockets.connect(
        ASSEMBLYAI_WS_URL,
        additional_headers={"Authorization": os.environ["ASSEMBLYAI_API_KEY"]},
    ) as aai_ws:

        async def forward_audio_to_assemblyai():
            """Read audio from Twilio, forward raw mulaw bytes to AssemblyAI."""
            nonlocal stream_sid, greeting_task
            try:
                async for raw in ws.iter_text():
                    msg = json.loads(raw)
                    event = msg.get("event")

                    if event == "start":
                        stream_sid = msg["start"]["streamSid"]
                        print(f"Stream started: {stream_sid}")
                        # Greet the caller in the agent's own voice
                        greeting_task = asyncio.create_task(
                            speak_on_call(ws, tts, stream_sid, GREETING)
                        )

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
                        # Prime the model with the greeting the caller hears first
                        await send_agent_context(aai_ws, GREETING)

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

                            # Tell AssemblyAI what the agent just said so the
                            # caller's next turn is transcribed in context
                            await send_agent_context(aai_ws, reply)

                            # Synthesise and inject audio back into the call
                            if stream_sid:
                                if greeting_task:
                                    await greeting_task  # one reply at a time on the session
                                await speak_on_call(ws, tts, stream_sid, reply)

            except Exception as e:
                print(f"AssemblyAI receive error: {e}")

        async def keep_tts_alive():
            """The TTS session closes after 2 idle minutes; ping it during long silences."""
            while True:
                await asyncio.sleep(60)
                await tts.keep_alive()

        keepalive = asyncio.create_task(keep_tts_alive())
        try:
            # Run both coroutines concurrently
            await asyncio.gather(
                forward_audio_to_assemblyai(),
                receive_transcripts_and_respond(),
            )
        finally:
            keepalive.cancel()


async def send_agent_context(aai_ws, text: str):
    """Push the agent's last reply mid-stream so the next caller turn is
    transcribed in context (e.g. primed for an account number)."""
    await aai_ws.send(json.dumps({
        "type": "UpdateConfiguration",
        "agent_context": text,
    }))


async def generate_llm_response(messages: list[dict]) -> str:
    """Call OpenAI GPT-4o with the conversation history."""
    response = await openai_client.chat.completions.create(
        model="gpt-4o",
        messages=messages,
        max_tokens=150,
        temperature=0.7,
    )
    return response.choices[0].message.content.strip()


async def speak_on_call(ws: WebSocket, tts: aai.AsyncTTSSession, stream_sid: str, text: str):
    """Speak one reply on the call's TTS session and stream it into the Twilio call."""
    async for chunk in tts.speak(text):  # raw 8 kHz mu-law chunks
        await ws.send_text(json.dumps({
            "event": "media",
            "streamSid": stream_sid,
            "media": {"payload": base64.b64encode(chunk).decode()},
        }))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
