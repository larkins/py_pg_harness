"""Regression test: a chat message WITH an image attachment must not die
with "Working outside of application context".

That was the failure of 2026-10-22 15:33: the NDJSON streaming path runs
the loop in a daemon thread with no Flask app context, and the vision
path read current_app.config["HARNESS_UPLOAD_DIR"].

This test goes through the real HTTP API, uploads a real PNG, sends a
chat with the attachment, and checks that the turn actually produces
events (iterations / tool calls) rather than dying instantly.

Runs against the live harness as a throwaway user; cleans up after.

Environment:
    HARNESS_BASE_URL       base URL of the running harness
                           (default: http://127.0.0.1:5010)
    HARNESS_VISION_AGENT   the agent to test (default: agent)
    HARNESS_DB             the database name (default: parsed from
                           DATABASE_URL, else "harness")
    HARNESS_DB_SUPERUSER   the postgres superuser to run psql as
                           (default: postgres)
    HARNESS_PYTHON         the python interpreter of the app venv
                           (default: <repo>/.venv/bin/python3)
    HARNESS_TEST_EMAIL_DOMAIN  domain of the throwaway user
                           (default: example.com)
"""
import json
import os
import re
import secrets
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]

BASE = os.environ.get("HARNESS_BASE_URL", "http://127.0.0.1:5010")
AGENT = os.environ.get("HARNESS_VISION_AGENT", "agent")
if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", AGENT):
    # The agent name is used as a schema name in the cleanup SQL below.
    raise SystemExit(f"HARNESS_VISION_AGENT is not a valid identifier: {AGENT!r}")
EMAIL_DOMAIN = os.environ.get("HARNESS_TEST_EMAIL_DOMAIN", "example.com")
EMAIL = f"harness-vision-{secrets.token_hex(3)}@{EMAIL_DOMAIN}"
PASSWORD = secrets.token_urlsafe(12)
PY = os.environ.get("HARNESS_PYTHON", str(APP_ROOT / ".venv" / "bin" / "python3"))
DB_SUPERUSER = os.environ.get("HARNESS_DB_SUPERUSER", "postgres")


def db_name() -> str:
    """The database to talk to: HARNESS_DB, else the one in DATABASE_URL."""
    if os.environ.get("HARNESS_DB"):
        return os.environ["HARNESS_DB"]
    url = os.environ.get("DATABASE_URL", "")
    if "://" in url and "/" in url.split("://", 1)[1]:
        return url.split("://", 1)[1].split("/", 1)[1].split("?")[0]
    return "harness"


def psql(sql):
    return subprocess.run(
        ["sudo", "-n", "-u", DB_SUPERUSER, "psql", "-d", db_name(), "-tAc", sql],
        capture_output=True, text=True, check=True).stdout.strip().splitlines()[0].strip()


def agent_table(table: str) -> str:
    """A quoted "<schema>"."<table>" reference.

    The schema name is the agent name (validated as an identifier above),
    so quoting it is enough to keep the statement safe.
    """
    return f'"{AGENT}"."{table}"'


def hash_password(pw):
    return subprocess.run(
        [PY, "-c", "from werkzeug.security import generate_password_hash;"
         "import sys;print(generate_password_hash(sys.argv[1]))", pw],
        capture_output=True, text=True, check=True).stdout.strip()


# A REAL png (generated with PIL) carrying actual content, so the
# vision path runs on valid bytes and the answer is checkable. A
# hand-rolled 1x1 hex blob is easy to get wrong (bad CRC) and the API
# rejects it with a 400 that looks like a harness bug but is not.
def make_png() -> bytes:
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (120, 80), "white")
    d = ImageDraw.Draw(img)
    d.rectangle([10, 10, 109, 69], fill="navy")
    d.text((20, 35), "HELLO 42", fill="yellow")
    import io
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


PNG = make_png()


def http(path, *, body=None, cookie=None, method=None, raw=None, ctype=None):
    data = raw if raw is not None else (
        json.dumps(body).encode() if body is not None else None)
    headers = {}
    if ctype:
        headers["Content-Type"] = ctype
    elif body is not None:
        headers["Content-Type"] = "application/json"
    if cookie:
        headers["Cookie"] = cookie
    req = urllib.request.Request(BASE + path, data=data, headers=headers,
                                 method=method or ("POST" if data else "GET"))
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.read(), r.headers.get("Set-Cookie")
    except urllib.error.HTTPError as e:
        return e.code, e.read(), None


def main():
    h = hash_password(PASSWORD)
    uid = psql("INSERT INTO public.users (email, password_hash, display_name,"
               " role, is_active, updated_at) VALUES "
               f"('{EMAIL}','{h}','Vision Test','member',true,now()) RETURNING id;")
    print(f"1. throwaway user id={uid}")
    try:
        st, b, setc = http("/api/v1/auth/login",
                           body={"email": EMAIL, "password": PASSWORD})
        cookie = (setc or "").split(";")[0]
        print(f"2. login -> {st}")
        assert st == 200, b[:200]

        # multipart upload of the PNG
        boundary = "----harnesstest" + secrets.token_hex(6)
        parts = (f"--{boundary}\r\n"
                 'Content-Disposition: form-data; name="file"; '
                 'filename="test.png"\r\n'
                 "Content-Type: image/png\r\n\r\n").encode() + PNG + \
                f"\r\n--{boundary}--\r\n".encode()
        st, b, _ = http("/api/v1/uploads", raw=parts,
                        cookie=cookie, ctype=f"multipart/form-data; boundary={boundary}")
        print(f"3. upload -> {st}")
        assert st in (200, 201), b[:200]
        up = json.loads(b)
        att = {"url": up["url"], "filename": up["filename"],
               "content_type": up["content_type"], "size": up["size"]}
        print(f"   attachment: {att['url']}")

        events = []
        res = {}

        def chat():
            try:
                req = urllib.request.Request(
                    f"{BASE}/api/v1/entities/{AGENT}/chat",
                    data=json.dumps({"message":
                        "Describe what you can see in this image, in one "
                        "sentence.", "attachments": [att]}).encode(),
                    headers={"Content-Type": "application/json",
                             "Accept": "application/x-ndjson",
                             "Cookie": cookie})
                with urllib.request.urlopen(req, timeout=300) as r:
                    for raw in r:
                        line = raw.decode("utf-8", "replace").strip()
                        if not line or line.startswith("#"):
                            continue
                        try:
                            events.append(json.loads(line))
                        except Exception:
                            pass
                    res["bytes"] = True
            except urllib.error.HTTPError as e:
                res["http"] = e.code
                res["body"] = e.read()[:300].decode(errors="replace")
            except Exception as e:
                res["err"] = f"{type(e).__name__}: {e}"

        th = threading.Thread(target=chat, daemon=True)
        th.start()
        t0 = time.time()
        while th.is_alive() and time.time() - t0 < 180:
            time.sleep(3)
            st, b, _ = http(f"/api/v1/entities/{AGENT}/turns/active",
                            cookie=cookie)
            if st == 200:
                d = json.loads(b)
                if d.get("active"):
                    print(f"   ... active: iter={d.get('iter')} "
                          f"tool_calls={d.get('tool_calls')} "
                          f"events={len(d.get('events') or [])}")
        th.join(timeout=30)
        print(f"4. stream finished after {time.time() - t0:.0f}s: {res}")
        types = [e.get("type") for e in events]
        print(f"   events: {len(events)} -> {types[:6]} ... {types[-3:]}")
        errs = [e for e in events if e.get("type") == "error"]
        finals = [e for e in events if e.get("type") == "final"]
        print(f"   error events: {len(errs)}  final events: {len(finals)}")
        for e in errs:
            print(f"   !! {e.get('message')}")
        if finals:
            txt = (finals[0].get("response") or {}).get("assistant") or ""
            print(f"   FINAL ({len(txt)} chars): {txt[:220]!r}")

        # did the assistant see the image? (a vision answer mentions pixels)
        saw_image = any(k in (finals[0].get("response") or {}).get("assistant", "").lower()
                        for k in ("pixel", "image", "png", "1x1", "transparent", "square")) if finals else False
        print(f"5. assistant referenced the image content: {saw_image}")

        ok = bool(finals) and not errs
        print("\nRESULT:", "PASS - turn completed with an image attachment"
              if ok else "FAIL")
        return 0 if ok else 1
    finally:
        psql("DELETE FROM " + agent_table("turns") + " WHERE conversation_id IN "
             "(SELECT id FROM " + agent_table("conversations") + " WHERE user_id="
             + str(uid) + ");")
        psql("DELETE FROM " + agent_table("conversations") + " WHERE user_id="
             + str(uid) + ";")
        psql("DELETE FROM public.users WHERE id=" + str(uid) + ";")
        print(f"cleaned up user {uid}")


if __name__ == "__main__":
    raise SystemExit(main())
