"""Does Grok work for JARVIS? Runs the real prompt on Hinglish commands, a tool round trip, Hindi voice samples
and a transcription, and prints what came back with timings.

    python manage.py xai_check                 # commands + tool call + 3 Hindi voice samples + transcription
    python manage.py xai_check --search        # also one web-search question (costs about half a cent)
    python manage.py xai_check --realtime      # also a live speech-to-speech session (text in, audio out)
    python manage.py xai_check --clip me.wav   # also transcribe your own recording (Hinglish test)

Voice samples are written to backend/xai_samples/ so you can listen and pick a voice.
"""
import asyncio
import base64
import json
import time
import wave
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from core import ai_brain, services, xai
from core.models import User

HINGLISH = [
    "kal 4 baje Sharma ji ke saath meeting rakh do, Pune franchise ke baare mein",
    "Mansi ko bolo Friday ki Pune flights book kare",
    "Ramesh ko bolo kal 4 baje tak GST reconciliation bhej de",
    "Mehta wala call ho gaya",
    "Sharma wali meeting 5 baje kar do",
    "aaj mere paas kya kya hai?",
    "Hindi mein bolo",
    "yaad rakhna mera city Indore hai",
    "Akash ke saath kal meeting rakh do",
    "aaj Indore ka temperature kya hai",
]

HINDI_SAMPLE = "नमस्ते सर, मैं जार्विस हूँ। कल शाम चार बजे शर्मा जी के साथ मीटिंग है। क्या मैं इसे सेव कर दूँ?"
HINGLISH_SAMPLE = "Sir, kal 4 baje Sharma ji ke saath meeting hai. Save kar doon?"


class Command(BaseCommand):
    help = "Test the xAI (Grok) brain and voice with the key in .env."

    def add_arguments(self, parser):
        parser.add_argument("--search", action="store_true")
        parser.add_argument("--realtime", action="store_true")
        parser.add_argument("--clip", default="", help="Path to a WAV/MP3/M4A you recorded, to test transcription.")
        parser.add_argument("--no-voice", action="store_true", help="Skip text-to-speech and transcription.")

    def ok(self, text):
        self.stdout.write(self.style.SUCCESS("  ✔ " + text))

    def bad(self, text):
        self.stdout.write(self.style.ERROR("  ✘ " + text))

    def handle(self, *args, **opts):
        if not xai.configured():
            self.bad("XAI_API_KEY is not set in backend/.env")
            return
        self.stdout.write(f"Base URL {settings.XAI_BASE_URL} · command model {settings.XAI_COMMAND_MODEL} · smart model {settings.XAI_SMART_MODEL}")
        md = User.objects.filter(role=User.Role.MD).first()
        if not md:
            self.bad("No MD account in this database (run seed_demo first).")
            return

        self.commands(md)
        self.tool_round_trip()
        if opts["search"]:
            self.web_search()
        if not opts["no_voice"]:
            self.voices()
        if opts["clip"]:
            self.transcribe_clip(opts["clip"])
        if opts["realtime"]:
            self.realtime()
        self.stdout.write(f"\nZero data retention header on the last reply: {xai.LAST.get('zdr')}")

    # ------------------------------------------------------------------ the real command prompt, on Grok
    def commands(self, md):
        self.stdout.write(self.style.MIGRATE_HEADING("\n1) Hinglish commands through JARVIS's own prompt (structured JSON)"))
        ctx = services.ai_context(md)
        ctx["conversation"] = []
        total = 0.0
        for text in HINGLISH:
            started = time.monotonic()
            try:
                data = xai.responses(
                    model=settings.XAI_COMMAND_MODEL, reasoning_effort="none",
                    instructions=ai_brain.COMMAND_SYSTEM_PROMPT,
                    input=f"<context>\n{json.dumps(ctx, ensure_ascii=False, default=str)}\n</context>\n<instruction>\n{text}\n</instruction>",
                    json_schema=ai_brain.COMMAND_SCHEMA, schema_name="command", conversation_id="xai-check", max_output_tokens=1200,
                )
                ms = int((time.monotonic() - started) * 1000)
                total += ms
                cmd = json.loads(xai.output_text(data))
                bits = [f"{cmd.get('intent')}"]
                for k in ("title", "kind", "due_date", "due_time", "staff_name", "target_ref", "setting_key", "setting_value", "memory_text", "question", "view_kind"):
                    if cmd.get(k):
                        bits.append(f"{k}={cmd[k]}")
                if cmd.get("needs_clarification"):
                    bits.append(f"ASKS: {cmd.get('clarification_question')}")
                cached = xai.LAST.get("cached_tokens") or 0
                self.ok(f"{ms:>5} ms · cached {cached:>5} · “{text}”\n        → " + " · ".join(bits) + f"\n        says: {cmd.get('spoken_reply')}")
            except Exception as err:  # noqa: BLE001
                self.bad(f"“{text}”: {err}")
        self.stdout.write(f"  average {int(total / max(1, len(HINGLISH)))} ms per command")

    # ------------------------------------------------------------------ function calling
    def tool_round_trip(self):
        self.stdout.write(self.style.MIGRATE_HEADING("\n2) Function calling round trip"))
        tools = [{
            "type": "function", "name": "find_person",
            "description": "Look a person up in the company: staff, the PA, or an outside contact.",
            "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
        }]
        instructions = ("You are JARVIS, an executive assistant. Before scheduling anything with a person, call find_person. "
                        "If the person is unknown, ask the user who they are instead of scheduling.")
        try:
            started = time.monotonic()
            first = xai.responses(model=settings.XAI_COMMAND_MODEL, reasoning_effort="none", instructions=instructions,
                                  input="Akash ke saath kal 11 baje meeting rakh do", tools=tools, max_output_tokens=400)
            calls = xai.function_calls(first)
            if not calls:
                self.bad("the model did not call find_person; it said: " + xai.output_text(first)[:200])
                return
            self.ok(f"model called {calls[0]['name']}({calls[0]['arguments']}) in {int((time.monotonic() - started) * 1000)} ms")
            follow = xai.responses(
                model=settings.XAI_COMMAND_MODEL, reasoning_effort="none", instructions=instructions, tools=tools,
                input=[
                    {"role": "user", "content": "Akash ke saath kal 11 baje meeting rakh do"},
                    {"type": "function_call", "call_id": calls[0]["call_id"], "name": "find_person", "arguments": json.dumps(calls[0]["arguments"])},
                    {"type": "function_call_output", "call_id": calls[0]["call_id"],
                     "output": json.dumps({"found": False, "closest": ["Anil Verma (Sales)", "Kavita Singh (Franchise)"]})},
                ],
                max_output_tokens=300,
            )
            self.ok("after 'not found' it says: " + xai.output_text(follow)[:300])
        except Exception as err:  # noqa: BLE001
            self.bad(f"tool call failed: {err}")

    # ------------------------------------------------------------------ web search
    def web_search(self):
        self.stdout.write(self.style.MIGRATE_HEADING("\n3) Web search question (smart model)"))
        try:
            started = time.monotonic()
            data = xai.responses(model=settings.XAI_SMART_MODEL, reasoning_effort="low",
                                 instructions="Answer in one or two short sentences for an Indian executive; use Celsius and IST.",
                                 input="What is the weather in Indore right now?", tools=[{"type": "web_search"}], max_output_tokens=400)
            self.ok(f"{int((time.monotonic() - started) * 1000)} ms → {xai.output_text(data)[:300]}")
            self.stdout.write(f"     sources: {[c['url'] for c in xai.citations(data)]}")
        except Exception as err:  # noqa: BLE001
            self.bad(f"web search failed: {err}")

    # ------------------------------------------------------------------ voices
    def voices(self):
        self.stdout.write(self.style.MIGRATE_HEADING("\n4) Hindi voice samples (listen and choose) + transcription"))
        out = Path(settings.BASE_DIR) / "xai_samples"
        out.mkdir(exist_ok=True)
        try:
            names = xai.tts_voices()
            ids = [v.get("voice_id") or v.get("id") or v.get("name") for v in names] if isinstance(names, list) else []
            self.stdout.write(f"  voices available: {ids[:20]}")
        except Exception as err:  # noqa: BLE001
            self.stdout.write(f"  (could not list voices: {err})")
            ids = []
        first_file = None
        for voice in ["eve", "ara", "rex"]:
            try:
                started = time.monotonic()
                audio = xai.tts(HINDI_SAMPLE, voice=voice, language="hi")
                path = out / f"hindi-{voice}.mp3"
                path.write_bytes(audio)
                first_file = first_file or path
                self.ok(f"{voice}: {len(audio) // 1024} KB in {int((time.monotonic() - started) * 1000)} ms → {path}")
            except Exception as err:  # noqa: BLE001
                self.bad(f"{voice}: {err}")
        try:
            audio = xai.tts(HINGLISH_SAMPLE, voice="eve", language="auto")
            path = out / "hinglish-eve.mp3"
            path.write_bytes(audio)
            self.ok(f"Hinglish (auto language): {path}")
        except Exception as err:  # noqa: BLE001
            self.bad(f"Hinglish sample: {err}")
        if first_file:
            try:
                started = time.monotonic()
                result = xai.stt(first_file.read_bytes(), filename=first_file.name, language="hi",
                                 keyterms=["JARVIS", "Sharma", "Mansi"])
                self.ok(f"transcribed back in {int((time.monotonic() - started) * 1000)} ms: “{result.get('text')}” (language {result.get('language')})")
            except Exception as err:  # noqa: BLE001
                self.bad(f"transcription: {err}")

    def transcribe_clip(self, path: str):
        self.stdout.write(self.style.MIGRATE_HEADING(f"\n5) Your recording: {path}"))
        p = Path(path)
        if not p.exists():
            self.bad("file not found")
            return
        try:
            started = time.monotonic()
            result = xai.stt(p.read_bytes(), filename=p.name, keyterms=["JARVIS", "Mansi", "Sharma", "Mehta", "Ramesh"])
            self.ok(f"{int((time.monotonic() - started) * 1000)} ms · language {result.get('language')} · “{result.get('text')}”")
        except Exception as err:  # noqa: BLE001
            self.bad(f"transcription: {err}")

    # ------------------------------------------------------------------ live voice
    def realtime(self):
        self.stdout.write(self.style.MIGRATE_HEADING("\n6) Live speech-to-speech session (text in, audio out)"))
        try:
            import websockets  # noqa: F401
        except ImportError:
            self.bad("pip install websockets  (then run again)")
            return
        out = Path(settings.BASE_DIR) / "xai_samples"
        out.mkdir(exist_ok=True)
        try:
            summary = asyncio.run(self._realtime_session(out / "realtime-reply.wav"))
            self.ok(summary)
        except Exception as err:  # noqa: BLE001
            self.bad(f"realtime: {err}")

    async def _realtime_session(self, wav_path: Path) -> str:
        import websockets

        url = settings.XAI_BASE_URL.replace("https://", "wss://").replace("http://", "ws://") + f"/realtime?model={settings.XAI_VOICE_MODEL}"
        headers = {"Authorization": f"Bearer {settings.XAI_API_KEY}"}
        started = time.monotonic()
        first_audio = None
        audio = bytearray()
        transcript = ""
        events = 0
        async with websockets.connect(url, additional_headers=headers, max_size=None) as ws:
            await ws.send(json.dumps({"type": "session.update", "session": {
                "instructions": "You are JARVIS, the Managing Director's assistant. Reply briefly in Hindi, address him as Sir.",
                "voice": settings.XAI_TTS_VOICE, "turn_detection": None, "reasoning": {"effort": "none"},
                "audio": {"output": {"format": {"type": "audio/pcm", "rate": 24000}}},
            }}))
            await ws.send(json.dumps({"type": "conversation.item.create", "item": {
                "type": "message", "role": "user", "content": [{"type": "input_text", "text": "Kal 4 baje Sharma ji ke saath meeting rakh do."}]}}))
            await ws.send(json.dumps({"type": "response.create"}))
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=40)
                events += 1
                if isinstance(raw, bytes):
                    audio.extend(raw)
                    first_audio = first_audio or time.monotonic()
                    continue
                msg = json.loads(raw)
                t = msg.get("type", "")
                if t == "response.output_audio.delta":
                    audio.extend(base64.b64decode(msg.get("delta", "")))
                    first_audio = first_audio or time.monotonic()
                elif t in ("response.output_audio_transcript.done", "response.audio_transcript.done"):
                    transcript = msg.get("transcript", "")
                elif t == "error":
                    raise RuntimeError(json.dumps(msg.get("error", msg))[:300])
                elif t == "response.done":
                    break
        with wave.open(str(wav_path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(24000)
            w.writeframes(bytes(audio))
        secs = len(audio) / 2 / 24000
        ttfa = int((first_audio - started) * 1000) if first_audio else -1
        return f"{events} events · first audio after {ttfa} ms · {secs:.1f} s of speech → {wav_path}\n        it said: {transcript[:300]}"
