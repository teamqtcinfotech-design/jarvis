"""The walkie-talkie relay: one open line per phone, one speaker at a time, sound forwarded as it arrives.

Run by ``manage.py walkie_server`` (its own process under PM2; nginx passes /ws/ to it). Each phone's always-on
part keeps a WebSocket open here, signed in with its login token (``?token=``). Text frames carry the small control
messages below; binary frames carry the sound (20 ms pieces of AMR-WB or raw PCM, opaque to the relay) and are
passed straight on to everyone in the talk. Nothing is stored except who talked to whom and for how long.

Phone -> relay (text, JSON):
  {"t":"hello"}                          who am I, who is around -> "ready"
  {"t":"status"}                         the people list again -> "people"
  {"t":"talk","to":[ids],"codec":"amrwb"}  hold to talk to these people (a new talk)
  {"t":"talk","talk":"<id>"}             hold to reply inside that talk
  {"t":"end"}                            let go
  {"t":"quiet","minutes":30}             quiet for a while (0 = quiet off)
  {"t":"ping"}                           -> "pong"
Relay -> phone:
  {"t":"ready","you":id,"people":[...]}  {"t":"people","people":[...]}  {"t":"presence","people":[...]}
  {"t":"floor","talk":id,"to":[ids],"waking":[names],"quiet":[names]}   you may speak (the sound may already flow)
  {"t":"busy","who":name}                somebody else is talking to one of them: wait
  {"t":"refused","why":...}
  {"t":"incoming","talk":id,"from":{"id","name"},"to":[ids],"codec":...}  somebody is talking to you: play
  {"t":"done","talk":id}                 they let go
  {"t":"ended","talk":id}                the talk is over (no reply came for a while)
A phone that is not connected is woken with a push ("walkie") and joins the talk when its line opens.
"""
import asyncio
import json
import logging
import time
import uuid
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

from asgiref.sync import sync_to_async
from django.utils import timezone

log = logging.getLogger("jarvis.walkie")

MAX_PRESS_SECONDS = 60      # one press ends by itself after this long
REPLY_WINDOW_SECONDS = 90   # a talk stays open for replies this long after the last press
MAX_FRAME_BYTES = 4096      # one binary message (a few 20 ms pieces); anything bigger is dropped
MAX_TO = 40


def _now():
    return timezone.now()


class Conn:
    """One phone's open line."""

    def __init__(self, ws, user):
        self.ws = ws
        self.user = user
        self.id = uuid.uuid4().hex[:8]
        self.codec = "amrwb"

    async def send(self, msg: dict) -> None:
        try:
            await self.ws.send(json.dumps(msg, ensure_ascii=False))
        except Exception:  # noqa: BLE001 — a dead line is cleaned up by its own loop
            pass

    async def send_bytes(self, data: bytes) -> None:
        try:
            await self.ws.send(data)
        except Exception:  # noqa: BLE001
            pass


class Talk:
    def __init__(self, talk_id: str, starter_id: int, members: set[int], codec: str):
        self.id = talk_id
        self.starter_id = starter_id
        self.members = members           # user ids, the starter included
        self.codec = codec
        self.speaker: Conn | None = None
        self.press_started = 0.0
        self.last_end = time.monotonic()
        self.turns = 0
        self.seconds = 0
        self.started_at = _now()
        self.press_timer: asyncio.Task | None = None


class Hub:
    """Everything the relay knows, in memory (one process serves the whole team)."""

    def __init__(self):
        self.conns: dict[int, set[Conn]] = {}      # user id -> their open lines (a phone and a tablet, say)
        self.talks: dict[str, Talk] = {}
        self.in_talk: dict[int, str] = {}           # user id -> the talk they are in right now
        self.lock = asyncio.Lock()
        self.pusher = None                           # set by serve(): (user, data) -> None, run in a thread
        self.noter = None                            # set by serve(): a "tried to reach you" note for a quiet person

    # ------------------------------------------------------------------ people
    async def _people(self, me) -> list[dict]:
        from . import services  # noqa: PLC0415
        from .models import User  # noqa: PLC0415

        def read():
            out = []
            now = _now()
            for u in User.objects.filter(is_active=True).exclude(id=me.id).order_by("id"):
                prefs = services.get_prefs(u)
                quiet_until = _parse(prefs.get("walkieQuietUntil"))
                if not prefs.get("walkieEnabled"):
                    status = "off"
                elif quiet_until and quiet_until > now:
                    status = "quiet"
                elif self.conns.get(u.id):
                    status = "available"
                else:
                    status = "reach"  # on, but the line is down: a push will try to wake it
                out.append({"id": u.id, "name": u.spoken_name, "title": u.title_label, "role": u.role, "status": status,
                            "quiet_until": quiet_until.isoformat() if quiet_until and quiet_until > now else "",
                            "talking": u.id in self.in_talk})
            return out
        return await sync_to_async(read)()

    async def broadcast_presence(self) -> None:
        for uid, conns in list(self.conns.items()):
            for c in list(conns):
                await c.send({"t": "presence", "people": await self._people(c.user)})

    async def _mark(self, user, connected: bool) -> None:
        from .models import WalkiePresence  # noqa: PLC0415

        def write():
            WalkiePresence.objects.update_or_create(user=user, defaults={"connected": connected, "last_seen": _now()})
        try:
            await sync_to_async(write)()
        except Exception:  # noqa: BLE001
            log.exception("presence not written")

    # ------------------------------------------------------------------ lines
    async def join(self, conn: Conn) -> None:
        async with self.lock:
            self.conns.setdefault(conn.user.id, set()).add(conn)
        await self._mark(conn.user, True)
        await conn.send({"t": "ready", "you": conn.user.id, "people": await self._people(conn.user)})
        # Joining a talk that was started for this person while the line was down: they hear it from here on.
        talk_id = self.in_talk.get(conn.user.id)
        talk = self.talks.get(talk_id) if talk_id else None
        if talk and talk.speaker is not None and talk.speaker.user.id != conn.user.id:
            await conn.send({"t": "incoming", "talk": talk.id, "from": {"id": talk.speaker.user.id, "name": talk.speaker.user.spoken_name},
                             "to": sorted(talk.members), "codec": talk.codec})
        await self.broadcast_presence()

    async def leave(self, conn: Conn) -> None:
        async with self.lock:
            lines = self.conns.get(conn.user.id)
            if lines:
                lines.discard(conn)
                if not lines:
                    del self.conns[conn.user.id]
            gone = not self.conns.get(conn.user.id)
        for talk in list(self.talks.values()):
            if talk.speaker is conn:
                await self.end(conn)
        if gone:
            await self._mark(conn.user, False)
        await self.broadcast_presence()

    # ------------------------------------------------------------------ talking
    async def talk(self, conn: Conn, msg: dict) -> None:
        from . import services  # noqa: PLC0415
        from .models import User  # noqa: PLC0415

        me = conn.user
        codec = msg.get("codec") if msg.get("codec") in ("amrwb", "pcm16") else "amrwb"
        talk_id = str(msg.get("talk") or "")
        talk = self.talks.get(talk_id) if talk_id else None
        if talk is None:
            # A new talk to the people named.
            prefs_md = await sync_to_async(lambda: services.get_prefs(services.get_md()) if services.get_md() else {})()
            if not me.is_md and not prefs_md.get("walkieAnyoneStarts", True):
                await conn.send({"t": "refused", "why": f"only {await sync_to_async(services.md_address)()} can start a talk"})
                return
            ids = [int(i) for i in (msg.get("to") or []) if str(i).isdigit()][:MAX_TO]
            people = await sync_to_async(lambda: list(User.objects.filter(id__in=ids, is_active=True).exclude(id=me.id)))()
            if not people:
                await conn.send({"t": "refused", "why": "say who to talk to"})
                return
            members = {me.id} | {u.id for u in people}
            # Someone already in another live talk: busy (they would hear two voices at once).
            for u in people:
                other = self.in_talk.get(u.id)
                if other and other in self.talks and self.talks[other].speaker is not None:
                    await conn.send({"t": "busy", "who": u.spoken_name})
                    return
            talk = Talk(uuid.uuid4().hex, me.id, members, codec)
            self.talks[talk.id] = talk
            asyncio.create_task(self._session_start(talk))
        else:
            if me.id not in talk.members:
                await conn.send({"t": "refused", "why": "you are not in that talk"})
                return
            if talk.speaker is not None and talk.speaker is not conn:
                await conn.send({"t": "busy", "who": talk.speaker.user.spoken_name})
                return
            people = await sync_to_async(lambda: list(User.objects.filter(id__in=talk.members, is_active=True).exclude(id=me.id)))()
        # The floor is theirs. The sound may already be flowing (the phone starts before this answer).
        talk.speaker = conn
        talk.press_started = time.monotonic()
        talk.turns += 1
        for uid in talk.members:
            self.in_talk[uid] = talk.id
        if talk.press_timer:
            talk.press_timer.cancel()
        talk.press_timer = asyncio.create_task(self._press_limit(talk, conn))
        waking, quiet, listening = [], [], []
        now = _now()
        for u in people:
            prefs = await sync_to_async(services.get_prefs)(u)
            quiet_until = _parse(prefs.get("walkieQuietUntil"))
            if not prefs.get("walkieEnabled"):
                continue  # switched off: they are not in it
            if quiet_until and quiet_until > now:
                quiet.append(u.spoken_name)
                if self.noter:
                    asyncio.create_task(self.noter(u, me, talk.id))
                continue
            lines = self.conns.get(u.id)
            if lines:
                listening.append(u.id)
                for c in list(lines):
                    await c.send({"t": "incoming", "talk": talk.id, "from": {"id": me.id, "name": me.spoken_name},
                                  "to": sorted(talk.members), "codec": talk.codec})
            else:
                waking.append(u.spoken_name)
                if self.pusher:
                    asyncio.create_task(self.pusher(u, {"type": "walkie", "talk": talk.id, "from": me.spoken_name, "from_id": str(me.id)}))
        await conn.send({"t": "floor", "talk": talk.id, "to": sorted(talk.members), "listening": listening, "waking": waking, "quiet": quiet})
        await self.broadcast_presence()

    async def audio(self, conn: Conn, data: bytes) -> None:
        if len(data) > MAX_FRAME_BYTES:
            return
        talk_id = self.in_talk.get(conn.user.id)
        talk = self.talks.get(talk_id) if talk_id else None
        if talk is None or talk.speaker is not conn:
            return  # not holding the floor: dropped (the phone sends from the first instant, before "floor")
        for uid in talk.members:
            if uid == conn.user.id:
                continue
            for c in list(self.conns.get(uid, ())):
                await c.send_bytes(data)

    async def end(self, conn: Conn) -> None:
        talk_id = self.in_talk.get(conn.user.id)
        talk = self.talks.get(talk_id) if talk_id else None
        if talk is None or talk.speaker is not conn:
            return
        talk.seconds += int(time.monotonic() - talk.press_started)
        talk.speaker = None
        talk.last_end = time.monotonic()
        if talk.press_timer:
            talk.press_timer.cancel()
            talk.press_timer = None
        for uid in talk.members:
            if uid == conn.user.id:
                continue
            for c in list(self.conns.get(uid, ())):
                await c.send({"t": "done", "talk": talk.id})
        asyncio.create_task(self._session_turn(talk, conn.user))
        asyncio.create_task(self._close_later(talk))
        await self.broadcast_presence()

    async def quiet(self, conn: Conn, minutes: int) -> None:
        from . import services  # noqa: PLC0415
        minutes = max(0, min(int(minutes or 0), 24 * 60))
        until = (_now() + timedelta(minutes=minutes)).isoformat() if minutes else ""
        await sync_to_async(services.set_pref_quiet)(conn.user, "walkieQuietUntil", until)
        await conn.send({"t": "quiet", "until": until})
        await self.broadcast_presence()

    # ------------------------------------------------------------------ timers and the log
    async def _press_limit(self, talk: Talk, conn: Conn) -> None:
        try:
            await asyncio.sleep(MAX_PRESS_SECONDS)
        except asyncio.CancelledError:
            return
        if talk.speaker is conn:
            await conn.send({"t": "cut", "talk": talk.id})
            await self.end(conn)

    async def _close_later(self, talk: Talk) -> None:
        await asyncio.sleep(REPLY_WINDOW_SECONDS)
        if talk.speaker is not None or time.monotonic() - talk.last_end < REPLY_WINDOW_SECONDS - 1:
            return  # somebody spoke again meanwhile
        self.talks.pop(talk.id, None)
        for uid in list(talk.members):
            if self.in_talk.get(uid) == talk.id:
                del self.in_talk[uid]
            for c in list(self.conns.get(uid, ())):
                await c.send({"t": "ended", "talk": talk.id})
        asyncio.create_task(self._session_end(talk))
        await self.broadcast_presence()

    async def _session_start(self, talk: Talk) -> None:
        from .models import TalkSession  # noqa: PLC0415
        try:
            await sync_to_async(TalkSession.objects.create)(id=talk.id, started_by_id=talk.starter_id, members=sorted(talk.members))
        except Exception:  # noqa: BLE001
            log.exception("talk not logged")

    async def _session_turn(self, talk: Talk, speaker) -> None:
        from .models import TalkSession  # noqa: PLC0415
        try:
            await sync_to_async(TalkSession.objects.filter(id=talk.id).update)(turns=talk.turns, seconds=talk.seconds, last_speaker=speaker)
        except Exception:  # noqa: BLE001
            log.exception("talk turn not logged")

    async def _session_end(self, talk: Talk) -> None:
        from .models import TalkSession  # noqa: PLC0415
        try:
            await sync_to_async(TalkSession.objects.filter(id=talk.id).update)(ended_at=_now(), turns=talk.turns, seconds=talk.seconds)
        except Exception:  # noqa: BLE001
            log.exception("talk end not logged")

    # ------------------------------------------------------------------ one line's loop
    async def handle(self, ws, user) -> None:
        conn = Conn(ws, user)
        await self.join(conn)
        try:
            async for raw in ws:
                if isinstance(raw, (bytes, bytearray)):
                    await self.audio(conn, bytes(raw))
                    continue
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                t = msg.get("t")
                if t == "ping":
                    await conn.send({"t": "pong"})
                elif t in ("hello", "status"):
                    await conn.send({"t": "ready" if t == "hello" else "people", "you": user.id, "people": await self._people(user)})
                elif t == "talk":
                    await self.talk(conn, msg)
                elif t == "end":
                    await self.end(conn)
                elif t == "quiet":
                    await self.quiet(conn, msg.get("minutes") or 0)
        finally:
            await self.leave(conn)


def _parse(value):
    from datetime import datetime  # noqa: PLC0415
    try:
        return datetime.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


# ---------------------------------------------------------------------- signing in a line
async def user_for_token(token: str):
    """The signed-in person behind an access token, or None (expired, wrong, switched off)."""
    from rest_framework_simplejwt.exceptions import TokenError  # noqa: PLC0415
    from rest_framework_simplejwt.tokens import AccessToken  # noqa: PLC0415
    from .models import User  # noqa: PLC0415

    def read():
        try:
            data = AccessToken(token)
        except TokenError:
            return None
        return User.objects.filter(id=data.get("user_id"), is_active=True).first()
    return await sync_to_async(read)()


async def serve(port: int) -> None:
    """Run the relay until stopped. Pushes and notes go through the usual services, in worker threads."""
    import websockets  # noqa: PLC0415
    from . import push, services  # noqa: PLC0415
    from .models import Alert, Priority  # noqa: PLC0415

    hub = Hub()

    async def pusher(user, data):
        try:
            await sync_to_async(push.send_to_user)(user, data, True)
        except Exception:  # noqa: BLE001
            log.exception("walkie wake push failed")

    async def noter(user, caller, talk_id):
        """A quiet person is told once, quietly, who tried to reach them."""
        def write():
            at = services.tu.fmt_time(_now())
            if Alert.objects.filter(recipient=user, ref_type="walkie", ref_id=talk_id).exists():
                return
            Alert.objects.create(recipient=user, sender=caller, kind=Alert.Kind.INFO, priority=Priority.LOW,
                                 title=f"{caller.spoken_name} tried the walkie-talkie", body=f"At {at}, while you were quiet.",
                                 spoken_text="", spoken_text_hi="", ref_type="walkie", ref_id=talk_id)
        try:
            await sync_to_async(write)()
        except Exception:  # noqa: BLE001
            log.exception("walkie note failed")

    hub.pusher, hub.noter = pusher, noter

    async def handler(ws):
        path = getattr(ws, "request", None)
        raw_path = path.path if path is not None else getattr(ws, "path", "")
        token = (parse_qs(urlparse(raw_path).query).get("token") or [""])[0]
        user = await user_for_token(token) if token else None
        if user is None:
            await ws.close(4401, "sign in again")
            return
        await hub.handle(ws, user)

    log.info("walkie relay on port %s", port)
    async with websockets.serve(handler, "0.0.0.0", port, ping_interval=20, ping_timeout=20, max_size=MAX_FRAME_BYTES * 4,
                                compression=None):
        await asyncio.Future()
