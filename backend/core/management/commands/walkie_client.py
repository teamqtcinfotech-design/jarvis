"""A second person on the walkie-talkie, played by the laptop, so one phone can test a two-way talk.

    python manage.py walkie_client                       # be "manager" (Mansi): echo back whatever is said, then answer
    python manage.py walkie_client --as pa --say "text"  # be the PA; answer with this sentence (Windows voice)
    python manage.py walkie_client --talk md             # start a talk TO the MD's phone (tests the incoming card
                                                         # and the wake push when the app is closed or the phone locked)
    python manage.py walkie_client --mode echo|say|both  # what to send back after the phone lets go (both)

It signs in with a token for that person, keeps a line open to the relay (JARVIS_WALKIE_URL), prints everything
that happens with timings, saves each incoming talk as an .awb file (playable in VLC / ffplay), and replies the way a
phone would: AMR-WB pieces, two per message, 40 ms apart. Nothing here is needed on the live server.
"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from core import services
from core.models import User

AMR_SIZES = [18, 24, 33, 37, 41, 47, 51, 59, 61, 6, 6, 6, 6, 6, 6, 1]  # AMR-WB frame sizes (TOC byte included), by mode
AWB_HEADER = b"#!AMR-WB\n"
FRAMES_PER_MESSAGE = 2       # 40 ms per WebSocket message, like the phone
DEFAULT_SAY = "Yes Sir, I heard you loud and clear. This is {name} on the walkie-talkie. The line is working."


def _stamp() -> str:
    return time.strftime("%H:%M:%S") + f".{int(time.time() * 1000) % 1000:03d}"


def _frames(data: bytes) -> list[bytes]:
    """Split AMR-WB storage-format bytes into frames (each starts with its TOC byte)."""
    out, pos = [], 0
    while pos < len(data):
        size = AMR_SIZES[(data[pos] >> 3) & 0x0F]
        out.append(data[pos:pos + size])
        pos += size
    return out


def make_voice(text: str, out_dir: str) -> bytes:
    """Say the text with the Windows voice, encode it as AMR-WB (ffmpeg), return the frames without the header."""
    wav = os.path.join(out_dir, "say.wav")
    awb = os.path.join(out_dir, "say.awb")
    script = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        "$s.Rate = 0; "
        f"$s.SetOutputToWaveFile('{wav}'); "
        f"$s.Speak('{text.replace(chr(39), chr(39) * 2)}'); "
        "$s.Dispose()"
    )
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], check=True, capture_output=True)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", wav, "-ar", "16000", "-ac", "1", "-c:a", "libvo_amrwbenc", "-b:a", "23850",
                    "-f", "amr", awb], check=True, capture_output=True)
    with open(awb, "rb") as f:
        data = f.read()
    if not data.startswith(AWB_HEADER):
        raise CommandError("ffmpeg did not write an AMR-WB file")
    return data[len(AWB_HEADER):]


class Line:
    def __init__(self, cmd, user: User, url: str, mode: str, say_text: str, out_dir: str):
        self.cmd = cmd
        self.user = user
        self.url = url
        self.mode = mode
        self.say_text = say_text
        self.out_dir = out_dir
        self.ws = None
        self.you = user.id
        self.people: list[dict] = []
        self.talk_id = ""
        self.incoming_from = ""
        self.buffer: list[bytes] = []
        self.first_byte_at = 0.0
        self.last_byte_at = 0.0
        self.count = 0
        self.floor = asyncio.Event()
        self.voice: bytes | None = None
        self.speaking = False

    def log(self, text: str) -> None:
        self.cmd.stdout.write(f"{_stamp()}  {text}")
        self.cmd.stdout.flush()

    async def send(self, msg: dict) -> None:
        await self.ws.send(json.dumps(msg))

    # ------------------------------------------------------------------ what the relay says
    async def on_text(self, raw: str) -> None:
        msg = json.loads(raw)
        t = msg.get("t")
        if t in ("ready", "people", "presence"):
            self.people = msg.get("people") or []
            line = ", ".join(f"{p['name']} {p['status']}{' (talking)' if p.get('talking') else ''}" for p in self.people) or "nobody else"
            self.log(f"{'READY as ' + self.user.spoken_name + ' · ' if t == 'ready' else 'people: '}{line}")
        elif t == "incoming":
            self.talk_id = msg["talk"]
            self.incoming_from = msg["from"]["name"]
            self.buffer, self.first_byte_at, self.last_byte_at = [], 0.0, 0.0
            self.log(f"◀ {self.incoming_from} is talking to me (talk {self.talk_id[:8]}, {msg.get('codec')})")
        elif t == "done":
            if self.buffer:
                heard = self.last_byte_at - self.first_byte_at
                frames = sum(len(_frames(b)) for b in self.buffer)
                self.log(f"  {self.incoming_from} let go: {frames} pieces = {frames * 20 / 1000:.1f} s of sound in {len(self.buffer)} messages, "
                         f"spread over {heard:.2f} s")
                self.count += 1
                path = os.path.join(self.out_dir, f"walkie-in-{self.count}.awb")
                with open(path, "wb") as f:
                    f.write(AWB_HEADER + b"".join(self.buffer))
                self.log(f"  saved {path}")
                asyncio.create_task(self.answer(list(self.buffer)))
            else:
                self.log(f"  {self.incoming_from} let go: no sound reached me")
        elif t == "floor":
            self.talk_id = msg["talk"]
            extra = []
            if msg.get("waking"):
                extra.append("waking " + ", ".join(msg["waking"]))
            if msg.get("quiet"):
                extra.append("quiet: " + ", ".join(msg["quiet"]))
            self.log(f"▶ floor is mine (talk {self.talk_id[:8]}; listening now: {msg.get('listening')}{'; ' + '; '.join(extra) if extra else ''})")
            self.floor.set()
        elif t == "busy":
            self.log(f"busy: {msg.get('who')} is talking")
            self.floor.set()
        elif t == "refused":
            self.log(f"refused: {msg.get('why')}")
            self.floor.set()
        elif t == "cut":
            self.log("cut: a press is at most a minute")
        elif t == "ended":
            self.log(f"talk {str(msg.get('talk'))[:8]} ended")
            if msg.get("talk") == self.talk_id:
                self.talk_id = ""
        elif t == "quiet":
            self.log(f"quiet until {msg.get('until') or 'now (off)'}")
        elif t != "pong":
            self.log(f"? {raw[:200]}")

    def on_bytes(self, data: bytes) -> None:
        now = time.monotonic()
        if not self.buffer:
            self.first_byte_at = now
            self.log(f"  first sound from {self.incoming_from}: {len(data)} bytes")
        self.last_byte_at = now
        self.buffer.append(data)

    # ------------------------------------------------------------------ talking back
    async def stream(self, messages: list[bytes]) -> None:
        """Send the pieces at the pace they were spoken (40 ms per message), like a phone's microphone would."""
        started = time.monotonic()
        sent_ms = 0
        for m in messages:
            await self.ws.send(m)
            sent_ms += len(_frames(m)) * 20
            wait = started + sent_ms / 1000 - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
        self.log(f"  sent {len(messages)} messages ({sent_ms / 1000:.1f} s) in {time.monotonic() - started:.2f} s")

    async def take_floor(self, to: list[int] | None) -> bool:
        self.floor.clear()
        msg = {"t": "talk", "codec": "amrwb"}
        if to:
            msg["to"] = to
        else:
            msg["talk"] = self.talk_id
        await self.send(msg)
        try:
            await asyncio.wait_for(self.floor.wait(), 5)
        except asyncio.TimeoutError:
            self.log("no answer to my press in 5 s")
            return False
        return True

    async def answer(self, heard: list[bytes]) -> None:
        if self.speaking:
            return
        self.speaking = True
        try:
            await asyncio.sleep(0.6)  # a breath, as a person would take
            if self.mode in ("echo", "both"):
                self.log(f"▶ replying with {self.incoming_from}'s own words (echo)")
                if await self.take_floor(None):
                    await self.stream(heard)
                    await self.send({"t": "end"})
                    self.log("  let go")
                await asyncio.sleep(0.8)
            if self.mode in ("say", "both"):
                if self.voice is None:
                    self.log("  making the voice…")
                    self.voice = make_voice(self.say_text, self.out_dir)
                self.log(f"▶ replying: “{self.say_text}”")
                if await self.take_floor(None):
                    frames = _frames(self.voice)
                    messages = [b"".join(frames[i:i + FRAMES_PER_MESSAGE]) for i in range(0, len(frames), FRAMES_PER_MESSAGE)]
                    await self.stream(messages)
                    await self.send({"t": "end"})
                    self.log("  let go")
        finally:
            self.speaking = False

    async def call(self, to: list[int]) -> None:
        """Start a talk from here to these people, with the sentence."""
        if self.voice is None:
            self.log("making the voice…")
            self.voice = make_voice(self.say_text, self.out_dir)
        names = ", ".join(p["name"] for p in self.people if p["id"] in to) or str(to)
        self.log(f"▶ talking to {names}: “{self.say_text}”")
        if await self.take_floor(to):
            frames = _frames(self.voice)
            messages = [b"".join(frames[i:i + FRAMES_PER_MESSAGE]) for i in range(0, len(frames), FRAMES_PER_MESSAGE)]
            await self.stream(messages)
            await self.send({"t": "end"})
            self.log("  let go; waiting for a reply (echoed back when it comes)")

    # ------------------------------------------------------------------ the loop
    async def run(self, token: str, talk_to: list[int]) -> None:
        from websockets.asyncio.client import connect  # noqa: PLC0415
        url = f"{self.url}?token={token}"
        self.log(f"connecting to {self.url} as {self.user.spoken_name} ({self.user.username})")
        async with connect(url, ping_interval=20, max_size=16384, compression=None) as ws:
            self.ws = ws
            await self.send({"t": "hello"})
            if talk_to:
                async def later():
                    await asyncio.sleep(0.8)
                    await self.call(talk_to)
                asyncio.create_task(later())
            async for raw in ws:
                if isinstance(raw, (bytes, bytearray)):
                    self.on_bytes(bytes(raw))
                else:
                    await self.on_text(raw)
        self.log("line closed")


class Command(BaseCommand):
    help = "Be a second person on the walkie-talkie from the laptop (echo / answer / call the phone)."

    def add_arguments(self, parser):
        parser.add_argument("--as", dest="who", default="manager", help="username of the person to be (default manager = Mansi)")
        parser.add_argument("--url", default="", help="relay address (default JARVIS_WALKIE_URL or ws://localhost:8001/ws/walkie/)")
        parser.add_argument("--mode", choices=["echo", "say", "both", "listen"], default="both", help="what to send back after they let go")
        parser.add_argument("--say", default="", help="the sentence to answer with (Windows voice)")
        parser.add_argument("--talk", default="", help="start a talk TO this username (or several, comma separated) as soon as the line is open")
        parser.add_argument("--out", default="", help="folder for the saved incoming .awb files (default: a temp folder)")

    def handle(self, *args, **opts):
        user = User.objects.filter(username=opts["who"], is_active=True).first()
        if user is None:
            raise CommandError(f"no active user '{opts['who']}'; users: " + ", ".join(User.objects.values_list("username", flat=True)))
        url = opts["url"] or getattr(settings, "JARVIS_WALKIE_URL", "") or "ws://localhost:8001/ws/walkie/"
        if not url.endswith("/walkie/"):
            url = url.rstrip("/") + "/ws/walkie/"
        out_dir = opts["out"] or os.path.join(tempfile.gettempdir(), "jarvis-walkie")
        os.makedirs(out_dir, exist_ok=True)
        # This person has the walkie-talkie on (else the relay leaves them out of every talk).
        if not services.get_prefs(user).get("walkieEnabled"):
            services.set_prefs(user, {"walkieEnabled": True})
            self.stdout.write(f"switched the walkie-talkie on for {user.spoken_name}")
        talk_to = []
        for name in [n.strip() for n in opts["talk"].split(",") if n.strip()]:
            other = User.objects.filter(username=name, is_active=True).first()
            if other is None:
                raise CommandError(f"no active user '{name}'")
            talk_to.append(other.id)
        say_text = opts["say"] or DEFAULT_SAY.format(name=user.spoken_name)
        from rest_framework_simplejwt.tokens import AccessToken  # noqa: PLC0415
        token = str(AccessToken.for_user(user))
        line = Line(self, user, url, opts["mode"], say_text, out_dir)
        if sys.platform == "win32":
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        try:
            asyncio.run(line.run(token, talk_to))
        except KeyboardInterrupt:
            self.stdout.write("bye")
