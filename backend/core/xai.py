"""Thin client for xAI (Grok): text (Responses API with tools and JSON output), text-to-speech, speech-to-text,
and short-lived tokens for the phone's live voice connection.

Only this module talks to api.x.ai. The key never leaves the server. Every reply's Zero-Data-Retention header is
remembered so the health check can show it.
"""
import json
import logging
import time

import requests
from django.conf import settings

log = logging.getLogger("jarvis.xai")

LAST = {"zdr": None, "at": None, "latency_ms": None, "model": "", "cached_tokens": 0, "error": ""}


class XaiError(Exception):
    def __init__(self, status: int, message: str, body=None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.body = body


def configured() -> bool:
    return bool(settings.XAI_API_KEY)


def _headers(extra: dict | None = None) -> dict:
    h = {"Authorization": f"Bearer {settings.XAI_API_KEY}", "Accept": "application/json"}
    if extra:
        h.update(extra)
    return h


def _remember(resp: requests.Response, started: float, model: str = "", usage: dict | None = None, error: str = ""):
    LAST.update(
        zdr=resp.headers.get("x-zero-data-retention") if resp is not None else None,
        at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        latency_ms=int((time.monotonic() - started) * 1000),
        model=model,
        cached_tokens=((usage or {}).get("input_tokens_details") or {}).get("cached_tokens", 0),
        error=error[:200],
    )


def _raise_for(resp: requests.Response):
    if 200 <= resp.status_code < 300:
        return
    try:
        body = resp.json()
    except ValueError:
        body = {"raw": resp.text[:500]}
    message = (body.get("error") or {}).get("message") if isinstance(body.get("error"), dict) else body.get("error") or body.get("message") or resp.text[:300]
    raise XaiError(resp.status_code, str(message), body)


# ------------------------------------------------------------------ text: Responses API
def responses(*, model: str, input, instructions: str | None = None, tools: list | None = None, tool_choice=None,
              json_schema: dict | None = None, schema_name: str = "result", reasoning_effort: str | None = None,
              max_output_tokens: int | None = None, conversation_id: str | None = None, store: bool = False,
              timeout: float | None = None) -> dict:
    """One call to POST /responses. Returns the raw response JSON (use output_text / function_calls on it)."""
    if not configured():
        raise XaiError(0, "XAI_API_KEY is not set.")
    body: dict = {"model": model, "input": input, "store": store}
    if instructions:
        body["instructions"] = instructions
    if tools:
        body["tools"] = tools
    if tool_choice is not None:
        body["tool_choice"] = tool_choice
    if json_schema:
        body["text"] = {"format": {"type": "json_schema", "name": schema_name, "schema": json_schema, "strict": True}}
    if reasoning_effort:
        body["reasoning"] = {"effort": reasoning_effort}
    if max_output_tokens:
        body["max_output_tokens"] = max_output_tokens
    headers = _headers({"Content-Type": "application/json"})
    if conversation_id:
        headers["x-grok-conv-id"] = conversation_id  # keeps the prompt cache warm across a conversation
    started = time.monotonic()
    resp = requests.post(f"{settings.XAI_BASE_URL}/responses", headers=headers, data=json.dumps(body),
                         timeout=timeout or settings.XAI_TIMEOUT_SECONDS)
    try:
        _raise_for(resp)
    except XaiError as err:
        _remember(resp, started, model, error=err.message)
        raise
    data = resp.json()
    _remember(resp, started, model, data.get("usage"))
    return data


def output_text(data: dict) -> str:
    parts = []
    for item in data.get("output", []):
        if item.get("type") == "message":
            for c in item.get("content", []):
                if c.get("type") in ("output_text", "text") and c.get("text"):
                    parts.append(c["text"])
    return "".join(parts).strip()


def function_calls(data: dict) -> list[dict]:
    out = []
    for item in data.get("output", []):
        if item.get("type") == "function_call":
            try:
                args = json.loads(item.get("arguments") or "{}")
            except ValueError:
                args = {}
            out.append({"call_id": item.get("call_id"), "name": item.get("name"), "arguments": args})
    return out


def citations(data: dict) -> list[dict]:
    seen, out = set(), []
    for item in data.get("output", []):
        for c in item.get("content", []) if item.get("type") == "message" else []:
            for a in c.get("annotations", []) or []:
                url = a.get("url")
                if url and url not in seen:
                    seen.add(url)
                    out.append({"title": (a.get("title") or url)[:120], "url": url})
    return out[:3]


# ------------------------------------------------------------------ voice
def tts(text: str, *, voice: str | None = None, language: str = "hi", codec: str = "mp3", sample_rate: int = 24000,
        speed: float = 1.0, timeout: float | None = None) -> bytes:
    """Text in, audio bytes out (MP3 by default)."""
    if not configured():
        raise XaiError(0, "XAI_API_KEY is not set.")
    body = {"text": text[:60000], "voice_id": voice or settings.XAI_TTS_VOICE, "language": language,
            "output_format": {"codec": codec, "sample_rate": sample_rate}, "speed": speed}
    started = time.monotonic()
    resp = requests.post(f"{settings.XAI_BASE_URL}/tts", headers=_headers({"Content-Type": "application/json"}),
                         data=json.dumps(body), timeout=timeout or settings.XAI_TIMEOUT_SECONDS)
    try:
        _raise_for(resp)
    except XaiError as err:
        _remember(resp, started, "tts", error=err.message)
        raise
    _remember(resp, started, "tts")
    return resp.content


# ---- streaming voice: one warm connection per server process, so speech starts in about half a second -----
_tts_lock = __import__("threading").Lock()
_tts_ws = None
_tts_key = None


def _tts_connect(voice: str, sample_rate: int):
    from websockets.sync.client import connect

    url = (settings.XAI_BASE_URL.replace("https://", "wss://").replace("http://", "ws://")
           + f"/tts?voice={voice}&language=auto&codec=pcm&sample_rate={sample_rate}")
    return connect(url, additional_headers={"Authorization": f"Bearer {settings.XAI_API_KEY}"}, open_timeout=8,
                   ping_interval=20, ping_timeout=20, max_size=None)


def tts_warm(voice: str | None = None, sample_rate: int = 24000) -> None:
    """Open the voice connection ahead of need (called when he starts talking), without blocking the caller."""
    global _tts_ws, _tts_key
    if not (configured() and getattr(settings, "JARVIS_GROK_VOICE", True)) or _tts_ws is not None:
        return

    def run():
        global _tts_ws, _tts_key
        if not _tts_lock.acquire(blocking=False):
            return  # someone is already speaking or connecting
        try:
            if _tts_ws is None:
                v = voice or settings.XAI_TTS_VOICE
                _tts_ws = _tts_connect(v, sample_rate)
                _tts_key = (v, sample_rate)
        except Exception:  # noqa: BLE001 — warming is best effort
            _tts_ws = None
        finally:
            _tts_lock.release()

    __import__("threading").Thread(target=run, name="jarvis-voice-warm", daemon=True).start()


def tts_stream(text: str, *, voice: str | None = None, sample_rate: int = 24000):
    """Yield raw 16-bit mono PCM chunks as Grok speaks them (Hindi and English on the same connection).

    The connection is kept open between sentences; a dead one is replaced once, silently. Raises XaiError before
    the first chunk if the voice cannot be reached, so the caller can fall back.
    """
    import base64

    global _tts_ws, _tts_key
    if not configured():
        raise XaiError(0, "XAI_API_KEY is not set.")
    voice = voice or settings.XAI_TTS_VOICE
    key = (voice, sample_rate)
    started = time.monotonic()
    with _tts_lock:
        last_error = None
        for attempt in (1, 2):
            try:
                if _tts_ws is None or _tts_key != key:
                    if _tts_ws is not None:
                        try:
                            _tts_ws.close()
                        except Exception:  # noqa: BLE001
                            pass
                    _tts_ws = _tts_connect(voice, sample_rate)
                    _tts_key = key
                ws = _tts_ws
                ws.send(json.dumps({"type": "text.delta", "delta": text[:60000]}))
                ws.send(json.dumps({"type": "text.done"}))
                first = True
                while True:
                    raw = ws.recv(timeout=20)
                    if isinstance(raw, bytes):
                        chunk = raw
                    else:
                        msg = json.loads(raw)
                        kind = msg.get("type")
                        if kind == "audio.done":
                            break
                        if kind == "error":
                            raise XaiError(502, str(msg.get("message") or msg.get("error") or msg)[:200])
                        if kind != "audio.delta":
                            continue
                        chunk = base64.b64decode(msg.get("delta") or "")
                    if not chunk:
                        continue
                    if first:
                        first = False
                        LAST.update(at=time.strftime("%Y-%m-%dT%H:%M:%S"), latency_ms=int((time.monotonic() - started) * 1000),
                                    model="tts-stream", error="")
                    yield chunk
                return
            except XaiError:
                raise
            except GeneratorExit:
                # The phone stopped listening mid-sentence: this connection still has audio queued, so drop it.
                try:
                    _tts_ws.close()
                except Exception:  # noqa: BLE001
                    pass
                _tts_ws = None
                raise
            except Exception as err:  # noqa: BLE001 — stale or broken socket: reconnect once
                last_error = err
                try:
                    if _tts_ws is not None:
                        _tts_ws.close()
                except Exception:  # noqa: BLE001
                    pass
                _tts_ws = None
                if attempt == 2 or not first:
                    break
        LAST.update(error=f"tts stream: {type(last_error).__name__}: {str(last_error)[:120]}")
        raise XaiError(502, f"voice stream failed: {last_error}")


def tts_voices() -> list:
    resp = requests.get(f"{settings.XAI_BASE_URL}/tts/voices", headers=_headers(), timeout=settings.XAI_TIMEOUT_SECONDS)
    _raise_for(resp)
    data = resp.json()
    return data.get("voices", data) if isinstance(data, dict) else data


def stt(audio: bytes, *, filename: str = "clip.wav", language: str | None = None, keyterms=(), timeout: float | None = None) -> dict:
    """Audio file bytes in, {"text", "language", "duration", "words"} out."""
    if not configured():
        raise XaiError(0, "XAI_API_KEY is not set.")
    fields: list[tuple] = [("model", (None, settings.XAI_STT_MODEL))]
    if language:
        fields.append(("language", (None, language)))
    for term in list(keyterms)[:100]:
        fields.append(("keyterm", (None, term[:50])))
    fields.append(("file", (filename, audio)))  # the file must come last
    started = time.monotonic()
    resp = requests.post(f"{settings.XAI_BASE_URL}/stt", headers=_headers(), files=fields,
                         timeout=timeout or max(settings.XAI_TIMEOUT_SECONDS, 60))
    try:
        _raise_for(resp)
    except XaiError as err:
        _remember(resp, started, settings.XAI_STT_MODEL, error=err.message)
        raise
    _remember(resp, started, settings.XAI_STT_MODEL)
    return resp.json()


def client_secret(seconds: int = 300) -> dict:
    """A short-lived token the phone uses to open the live voice connection itself (the key stays here)."""
    if not configured():
        raise XaiError(0, "XAI_API_KEY is not set.")
    resp = requests.post(f"{settings.XAI_BASE_URL}/realtime/client_secrets", headers=_headers({"Content-Type": "application/json"}),
                         data=json.dumps({"expires_after": {"seconds": int(seconds)}}), timeout=settings.XAI_TIMEOUT_SECONDS)
    _raise_for(resp)
    return resp.json()


def describe() -> str:
    if not configured():
        return "off (XAI_API_KEY not set)"
    zdr = LAST.get("zdr")
    return "on" + (f" · zero data retention {'ON' if zdr == 'true' else 'OFF' if zdr == 'false' else 'unknown'}")
