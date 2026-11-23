#!/usr/bin/env python3
"""Prove the TaskPanel API for JARVIS (contract v1) from the JARVIS VPS, before anything is built into the app.

    export TASKPANEL_API_TOKEN=<64 hex>  TASKPANEL_API_SECRET=<shared secret>
    python3 deploy/taskpanel_check.py --selftest        # signature against the contract's test vector, no network
    python3 deploy/taskpanel_check.py                   # the read-only checks: health, staff, tasks, events, datasets, reports, refusals
    python3 deploy/taskpanel_check.py --pilot <user_key> "Test task from JARVIS"   # ONE task, then the same call again: must replay

Prints one PASS / FAIL line per check. Never prints report rows (only counts and column names), so nothing personal
lands in a terminal log. Standard library only.
"""
import argparse
import hashlib
import hmac
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("TASKPANEL_API_BASE", "https://shivtrinetra.in/taskPanel/api.php")
PATH = urllib.parse.urlparse(BASE).path or "/taskPanel/api.php"
TOKEN = os.environ.get("TASKPANEL_API_TOKEN", "")
SECRET = os.environ.get("TASKPANEL_API_SECRET", "")
PREFIX = "JARVIS-HMAC-SHA256-V1"

TEST_VECTOR = {
    "secret": "test-secret-0123456789abcdef0123456789abcdef",
    "method": "GET", "query": {"route": "tasks", "status": "open", "page": "1"},
    "ts": "1795400000", "nonce": "abcdefghijklmnop1234", "body": b"",
    "canonical": "JARVIS-HMAC-SHA256-V1\nGET\n/taskPanel/api.php\npage=1&route=tasks&status=open\n1795400000\nabcdefghijklmnop1234\n"
                 "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "signature": "09838672b311004d23c70483f6f63bd389ede655bb60fd9c5ca8b6cacbcfac40",
}


def canonical(method: str, query: dict, ts: str, nonce: str, raw: bytes, path: str = PATH) -> str:
    q = "&".join(f"{urllib.parse.quote(str(k), safe='')}={urllib.parse.quote(str(v), safe='')}" for k, v in sorted(query.items()))
    return "\n".join([PREFIX, method.upper(), path, q, ts, nonce, hashlib.sha256(raw).hexdigest()])


def sign(secret: str, text: str) -> str:
    return hmac.new(secret.encode(), text.encode(), hashlib.sha256).hexdigest()


def call(method: str, route: str, params: dict | None = None, body: dict | None = None, *, bad_signature: bool = False,
         nonce: str | None = None, token: str | None = None):
    """-> (http status, parsed JSON or None, headers)."""
    query = {"route": route, **(params or {})}
    raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode() if body is not None else b""
    ts = str(int(time.time()))
    nonce = nonce or secrets.token_urlsafe(18)
    sig = sign(SECRET, canonical(method, query, ts, nonce, raw))
    if bad_signature:
        sig = "0" * 64
    headers = {"X-Jarvis-Token": token if token is not None else TOKEN, "X-Jarvis-Timestamp": ts, "X-Jarvis-Nonce": nonce,
               "X-Jarvis-Signature": sig, "Accept": "application/json", "User-Agent": "jarvis-check/1"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    url = BASE + "?" + urllib.parse.urlencode(query, quote_via=urllib.parse.quote, safe="")
    req = urllib.request.Request(url, data=raw or None, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return r.status, _json(r.read()), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, _json(e.read()), dict(e.headers)
    except (urllib.error.URLError, TimeoutError) as e:
        return 0, {"ok": False, "error": {"code": "network", "message": str(e)}}, {}


def _json(data: bytes):
    try:
        return json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None


results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, note: str = "") -> bool:
    results.append((name, bool(ok), note))
    print(("PASS  " if ok else "FAIL  ") + name + (f"  —  {note}" if note else ""))
    return bool(ok)


def selftest() -> bool:
    v = TEST_VECTOR
    text = canonical(v["method"], v["query"], v["ts"], v["nonce"], v["body"])
    ok1 = check("canonical text matches the contract's test vector", text == v["canonical"])
    ok2 = check("signature matches the contract's test vector", sign(v["secret"], text) == v["signature"])
    return ok1 and ok2


def err(d) -> str:
    if not isinstance(d, dict):
        return "no JSON"
    e = d.get("error") or {}
    return f"{e.get('code', '?')}: {e.get('message', '')}".strip()


def reads() -> None:
    # 1. health: token + signature accepted, which address the server sees
    st, d, _h = call("GET", "health")
    data = (d or {}).get("data") or {}
    net = data.get("network") or {}
    check("health answers 200", st == 200 and (d or {}).get("ok") is True, err(d) if st != 200 else f"request_id {(d or {}).get('request_id')}")
    actor = data.get("actor") if isinstance(data.get("actor"), dict) else {"user_key": data.get("actor") or data.get("account")}
    expected = os.environ.get("TASKPANEL_EXPECTED_ACTOR", "bijender")
    check("health: token acts as the MD's account", (actor.get("user_key") or "") == expected,
          f"actor.user_key {actor.get('user_key')} (expected {expected}) | role {actor.get('role')}")
    check("health: our address is allowed", net.get("ip_allowed") is True,
          f"remote_addr {net.get('remote_addr')} | cf_connecting_ip {net.get('cf_connecting_ip')} | resolved {net.get('resolved_ip')} | via_cloudflare {net.get('via_cloudflare')}")
    check("health: scopes present", bool(data.get("scopes")), f"scopes {data.get('scopes')} | writes_enabled {data.get('writes_enabled')} | server_time {data.get('server_time')}")
    # 2. staff + categories
    st, d, _h = call("GET", "staff")
    data = (d or {}).get("data") or {}
    staff, cats = data.get("staff") or [], data.get("categories") or []
    check("staff list returned", st == 200 and len(staff) > 0, err(d) if st != 200 else f"{len(staff)} people; first: " + ", ".join(s.get("name", "?") for s in staff[:3]))
    check("staff rows carry key, dept, role, leave", all(k in staff[0] for k in ("user_key", "name", "dept", "role", "active", "on_leave")) if staff else False,
          ", ".join(sorted(staff[0].keys())) if staff else "")
    other = next((c for c in cats if c.get("key") == "other"), None)
    check("28 categories with department rules", len(cats) >= 20 and other is not None,
          f"{len(cats)} categories; 'other' assignable to {len((other or {}).get('assignable_to') or [])} people")
    # 3. tasks
    st, d, _h = call("GET", "tasks", {"status": "all", "page": "1", "page_size": "5"})
    data = (d or {}).get("data") or {}
    tasks = data.get("tasks") or []
    check("tasks list returned", st == 200 and isinstance(tasks, list), err(d) if st != 200 else f"total {data.get('total')} | page {data.get('page')}/{data.get('pages')}")
    first_id = (tasks[0] or {}).get("id") if tasks else None
    if first_id:
        st, d, _h = call("GET", "task", {"id": str(first_id)})
        td = ((d or {}).get("data") or {})
        check("one task with its events", st == 200 and "events" in json.dumps(td)[:200000], err(d) if st != 200 else f"{first_id}: {len(td.get('events') or (td.get('task') or {}).get('events') or [])} events")
    else:
        check("one task with its events", True, "no tasks yet: skipped")
    # 4. events feed
    st, d, _h = call("GET", "task.events", {"since_id": "0", "limit": "5"})
    data = (d or {}).get("data") or {}
    check("task events feed", st == 200 and "next_since_id" in data, err(d) if st != 200 else f"{len(data.get('events') or [])} events | next_since_id {data.get('next_since_id')}")
    # 5. datasets + two reports (franchise, partner): counts and column names only
    st, d, _h = call("GET", "datasets")
    data = (d or {}).get("data") or {}
    kinds = data.get("kinds") or []
    check("dataset kinds listed", st == 200 and len(kinds) > 0, err(d) if st != 200 else f"{len(kinds)} kinds; never served: {len(data.get('never_served') or [])}")
    for kind in ("franchises", "partners"):
        # Contract v1 as confirmed: rows are data.records, columns data.columns[{key,label,number}], hidden ones counted.
        st, d, _h = call("GET", "report", {"kind": kind, "result_mode": "list", "page": "1"})
        data = (d or {}).get("data") or {}
        rows = data.get("records") or []
        cols = [c.get("key") for c in (data.get("columns") or []) if isinstance(c, dict)] or sorted({k for r in rows if isinstance(r, dict) for k in r.keys()})
        leak = [c for c in cols if any(w in str(c).lower() for w in ("pan", "aadhaar", "aadhar", "account", "ifsc", "bank", "phone", "mobile", "nominee"))]
        check(f"report {kind} returned", st == 200 and isinstance(rows, list),
              err(d) if st != 200 else f"{len(rows)} of {data.get('total')} records on page 1; hidden columns {data.get('hidden_columns')}; columns: {', '.join(map(str, cols))[:200]}")
        check(f"report {kind} has no sensitive columns", not leak, ", ".join(map(str, leak)))
        st, d, _h = call("GET", "report", {"kind": kind, "result_mode": "count"})
        data = (d or {}).get("data") or {}
        check(f"report {kind} count", st == 200 and isinstance(data.get("total"), int), err(d) if st != 200 else f"total {data.get('total')}")
    # 6. refusals
    st, d, _h = call("GET", "staff", bad_signature=True)
    check("a wrong signature is refused", st == 401, err(d))
    n = secrets.token_urlsafe(18)
    call("GET", "health", nonce=n)
    st, d, _h = call("GET", "health", nonce=n)
    check("a reused nonce is refused (replay)", st == 401, err(d))
    st, d, _h = call("GET", "staff", token="0" * 64)
    check("a wrong token is refused", st == 401, err(d))
    st, d, _h = call("POST", "task.create", body={"command_id": "probe-" + secrets.token_hex(4), "cat": "other", "title": "x", "to": "nobody"})
    check("task.create is refused while the token is read-only", st in (403, 503) and (d or {}).get("ok") is False, err(d))


def staff_list() -> None:
    """The whole staff directory and the categories, as TaskPanel gives them to the MD's token."""
    st, d, _h = call("GET", "staff")
    if st != 200:
        print(f"staff: HTTP {st} {err(d)}")
        return
    data = (d or {}).get("data") or {}
    staff, cats = data.get("staff") or [], data.get("categories") or []
    print(f"{len(staff)} staff\n")
    print(f"{'user_key':<14} {'name':<30} {'dept':<16} {'level':<10} {'role':<8} {'active':<7} {'on_leave':<9} {'leave_until':<12} open")
    for s in sorted(staff, key=lambda s: (str(s.get('dept') or ''), str(s.get('name') or ''))):
        print(f"{str(s.get('user_key') or ''):<14} {str(s.get('name') or '')[:30]:<30} {str(s.get('dept') or '')[:16]:<16} "
              f"{str(s.get('level_name') or s.get('level') or '')[:10]:<10} {str(s.get('role') or ''):<8} {str(s.get('active')):<7} "
              f"{str(s.get('on_leave')):<9} {str(s.get('leave_until') or ''):<12} {s.get('open_tasks', '')}")
    print(f"\n{len(cats)} categories\n")
    print(f"{'key':<28} {'name':<34} {'dept':<16} {'priority':<9} assignable")
    for c in sorted(cats, key=lambda c: (str(c.get('dept') or ''), str(c.get('key') or ''))):
        print(f"{str(c.get('key') or ''):<28} {str(c.get('name') or '')[:34]:<34} {str(c.get('dept') or '')[:16]:<16} "
              f"{str(c.get('default_priority') or ''):<9} {len(c.get('assignable_to') or [])}")


def pilot(to_key: str, title: str) -> None:
    cid = "pilot-" + secrets.token_hex(6)
    body = {"command_id": cid, "cat": "other", "title": title, "to": to_key, "pri": "normal", "dueKind": "tmrw",
            "remark": "Pilot task created by JARVIS; please start and complete it in TaskPanel."}
    st, d, _h = call("POST", "task.create", body=body)
    data = (d or {}).get("data") or {}
    tid = data.get("id") or (d or {}).get("id")
    check("pilot task created (201)", st == 201 and bool(tid), err(d) if st != 201 else f"{tid} | replayed {data.get('replayed', (d or {}).get('replayed'))}")
    st2, d2, _h = call("POST", "task.create", body=body)
    data2 = (d2 or {}).get("data") or {}
    check("the same call again replays, no second task", st2 == 200 and (data2.get("replayed") or (d2 or {}).get("replayed")) is True
          and (data2.get("id") or (d2 or {}).get("id")) == tid, err(d2) if st2 not in (200, 201) else f"{data2.get('id') or (d2 or {}).get('id')}")
    st3, d3, _h = call("POST", "task.create", body={**body, "title": title + " (changed)"})
    check("same command_id with different content is refused (409)", st3 == 409, err(d3))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--selftest", action="store_true", help="only the signature test vector, no network")
    ap.add_argument("--pilot", nargs=2, metavar=("USER_KEY", "TITLE"), help="create ONE pilot task, then prove the replay")
    ap.add_argument("--staff", action="store_true", help="print the whole staff directory and the categories, nothing else")
    args = ap.parse_args()
    if not selftest():
        return 2
    if args.selftest:
        return 0
    if not (TOKEN and SECRET):
        print("set TASKPANEL_API_TOKEN and TASKPANEL_API_SECRET first", file=sys.stderr)
        return 2
    print(f"\nagainst {BASE}\n")
    if args.staff:
        staff_list()
        return 0
    reads()
    if args.pilot:
        print()
        pilot(args.pilot[0], args.pilot[1])
    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)} of {len(results)} checks passed" + (f"; failed: {', '.join(failed)}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
