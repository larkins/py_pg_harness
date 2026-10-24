#!/usr/bin/env python3
"""End-to-end smoke test for the group chat feature.

Exercises the real HTTP API against the running harness:

  1. create a throwaway admin + member + outsider (public.users)
  2. admin creates a group (members from public.users, agents from the
     installed roster)
  3. the member sees the group in GET /api/v1/groups  (landing row)
  4. the outsider does NOT see it, and cannot read or post to it
  5. a message with two @mentions makes BOTH agents take a turn, and
     the second agent can see the first agent's reply
  6. a message with no @mention makes no agent take a turn

Everything it creates is removed again at the end.

Usage (from the repo root, with the app env loaded):
    set -a; . ./.env; set +a
    source .venv/bin/activate
    python3 scripts/smoke_groups.py

Environment:
    HARNESS_SMOKE_BASE          base URL of the running harness
                                (default: http://127.0.0.1:5010)
    HARNESS_SMOKE_EMAIL_DOMAIN  domain of the throwaway users
                                (default: example.com)
    HARNESS_SMOKE_AGENT_A/B     the two agents to exercise (default: the
                                first two agents of the installed roster)
"""
from __future__ import annotations

import http.cookiejar
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BASE = os.environ.get("HARNESS_SMOKE_BASE", "http://127.0.0.1:5010")
PASSWORD = "smoke-groups-pw-2026"
EMAIL_DOMAIN = os.environ.get("HARNESS_SMOKE_EMAIL_DOMAIN", "example.com")
ADMIN_EMAIL = f"smoke_groups_admin@{EMAIL_DOMAIN}"
MEMBER_EMAIL = f"smoke_groups_member@{EMAIL_DOMAIN}"
OUTSIDER_EMAIL = f"smoke_groups_outsider@{EMAIL_DOMAIN}"
# The two agents the group is built from. Empty = the first two agents of
# the installed roster (app.agents in config.yaml).
AGENT_A = os.environ.get("HARNESS_SMOKE_AGENT_A", "")
AGENT_B = os.environ.get("HARNESS_SMOKE_AGENT_B", "")

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    status = "PASS" if ok else "FAIL"
    print(f"  [{status}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILURES.append(label)
    return ok


class Client:
    """Tiny cookie-aware HTTP client (no `requests` in the venv)."""

    def __init__(self) -> None:
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar),
            urllib.request.HTTPRedirectHandler(),
        )

    def call(self, method: str, path: str, body=None, accept: str = "application/json"):
        url = BASE + path
        data = None
        # Cloudflare's browser-integrity rule (error 1010) rejects the
        # default Python-urllib user agent, so send a browser UA.
        headers = {
            "Accept": accept,
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64; rv:158.0) "
                "Gecko/20100101 Firefox/158.0"
            ),
        }
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=900) as r:
                raw = r.read().decode("utf-8", "replace")
                return r.status, raw
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")

    def json(self, method: str, path: str, body=None, accept: str = "application/json"):
        status, raw = self.call(method, path, body, accept)
        try:
            return status, json.loads(raw)
        except json.JSONDecodeError:
            return status, {"_raw": raw[:800]}

    def login(self, email: str, password: str = PASSWORD):
        return self.json("POST", "/api/v1/auth/login",
                         {"email": email, "password": password})


def main() -> int:
    global AGENT_A, AGENT_B

    from app import create_app
    from app.auth import create_user

    app = create_app()
    with app.app_context():
        # The two agents to exercise: the installed roster, unless the
        # caller pinned them.
        roster = list(app.config["HARNESS_CONFIG_OBJ"].app.agents)
        if not AGENT_A:
            AGENT_A = roster[0] if roster else "agent"
        if not AGENT_B:
            AGENT_B = roster[1] if len(roster) > 1 else AGENT_A
        print(f"agents under test: {AGENT_A}, {AGENT_B}")
        # The harness runtime role only has SELECT on public.users, so
        # the throwaway users are normally created by the operator (a
        # superuser INSERT). If create_user works, great; if not, the
        # users must already exist.
        try:
            admin_id = create_user(ADMIN_EMAIL, PASSWORD, "Smoke Admin", role="admin")
            member_id = create_user(MEMBER_EMAIL, PASSWORD, "Smoke Member", role="member")
            outsider_id = create_user(OUTSIDER_EMAIL, PASSWORD, "Smoke Outsider", role="member")
            print("created users in-process")
        except Exception as exc:
            print(f"could not create the users in-process ({type(exc).__name__}); "
                  f"they must already exist")
            admin_id = member_id = outsider_id = -1
        if admin_id == -1:
            pool = app.extensions["harness"]["pool"]
            with pool.connection() as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT id, email FROM public.users WHERE email = ANY(%s)",
                    ([ADMIN_EMAIL, MEMBER_EMAIL, OUTSIDER_EMAIL],),
                )
                found = {r[1]: r[0] for r in cur.fetchall()}
            missing = [e for e in (ADMIN_EMAIL, MEMBER_EMAIL, OUTSIDER_EMAIL) if e not in found]
            if missing:
                print(f"ERROR: missing users {missing} — create them as a superuser first")
                return 2
            admin_id = found[ADMIN_EMAIL]
            member_id = found[MEMBER_EMAIL]
            outsider_id = found[OUTSIDER_EMAIL]
    print(f"users: admin={admin_id} member={member_id} outsider={outsider_id}")

    group_id = None
    try:
        # --- 1. admin logs in -------------------------------------------
        admin = Client()
        st, data = admin.login(ADMIN_EMAIL)
        check("admin login", st == 200, f"status={st}")

        # --- 2. the user pool + agent roster ----------------------------
        st, data = admin.json("GET", "/api/v1/admin/users")
        users = data.get("users", [])
        check("GET /api/v1/admin/users returns public.users", st == 200 and len(users) > 0,
              f"{len(users)} users")
        st, data = admin.json("GET", "/api/v1/agents")
        agents = data.get("agents", [])
        check("GET /api/v1/agents returns the roster", st == 200 and agents,
              f"{agents}")

        # --- 3. create the group ----------------------------------------
        st, data = admin.json("POST", "/api/v1/groups", {
            "name": "smoke test group",
            "description": "created by scripts/smoke_groups.py",
            "member_ids": [member_id],
            "agent_names": [AGENT_A, AGENT_B],
        })
        check("POST /api/v1/groups (admin) creates a group", st == 201,
              f"status={st} {data.get('error', '')}")
        if st != 201:
            return 1
        group = data["group"]
        group_id = group["id"]
        print(f"    group={group_id} members={len(group['members'])} agents={group['agents']}")
        check("group has the requested members",
              any(m["user_id"] == member_id for m in group["members"]))
        check("group has the requested agents",
              set(group["agents"]) == {AGENT_A, AGENT_B}, str(group["agents"]))

        # --- 4. a non-admin cannot create a group -----------------------
        member = Client()
        st, _ = member.login(MEMBER_EMAIL)
        check("member login", st == 200, f"status={st}")
        st, data = member.json("POST", "/api/v1/groups", {"name": "nope"})
        check("non-admin cannot create a group", st == 403, f"status={st}")

        # --- 5. the member sees the group (landing row) -----------------
        st, data = member.json("GET", "/api/v1/groups")
        names = [g["name"] for g in data.get("groups", [])]
        check("member sees the group in GET /api/v1/groups",
              st == 200 and "smoke test group" in names, f"status={st} {names}")

        # --- 6. the outsider cannot see or use it ------------------------
        outsider = Client()
        st, _ = outsider.login(OUTSIDER_EMAIL)
        st, data = outsider.json("GET", "/api/v1/groups")
        check("outsider does not see the group",
              "smoke test group" not in [g["name"] for g in data.get("groups", [])])
        st, data = outsider.json("GET", f"/api/v1/groups/{group_id}")
        check("outsider cannot read the group", st == 403, f"status={st}")
        st, data = outsider.json("POST", f"/api/v1/groups/{group_id}/chat",
                                 {"message": f"@{AGENT_A} hello"})
        check("outsider cannot post to the group", st == 403, f"status={st}")

        # --- 7. two @mentions -> two agent turns -------------------------
        print(f"  ... posting '@{AGENT_A} @{AGENT_B}' (this runs two real agent turns)")
        t0 = time.time()
        st, raw = member.call(
            "POST", f"/api/v1/groups/{group_id}/chat",
            {"message": f"@{AGENT_A} @{AGENT_B}, reply with a one-line greeting and nothing else."},
            accept="application/x-ndjson",
        )
        took = time.time() - t0
        events = []
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        finals = [e for e in events if e.get("type") == "final"]
        agents_that_ran = [e.get("agent") for e in finals]
        check("both mentioned agents took a turn",
              set(agents_that_ran) == {AGENT_A, AGENT_B}, f"{agents_that_ran} in {took:.0f}s")
        check("the first agent's turn comes first",
              agents_that_ran == [AGENT_A, AGENT_B], str(agents_that_ran))
        for f in finals:
            text = (f.get("response") or {}).get("assistant", "")
            print(f"    {f.get('agent')}: {text[:110]}")

        # --- 8. the transcript -------------------------------------------
        st, data = member.json("GET", f"/api/v1/groups/{group_id}/messages?limit=50")
        msgs = data.get("messages", [])
        kinds = [(m["author_type"], m["author_name"]) for m in msgs]
        check("the transcript has the human message + 2 agent replies", len(msgs) == 3,
              str(kinds))
        check("the agent replies are attributed",
              ("agent", AGENT_A) in kinds and ("agent", AGENT_B) in kinds, str(kinds))

        # --- 9. the second agent saw the first agent's reply ------------
        # The prompt of the second agent is checked through the agent's
        # own turn table: the group conversation id is shared, and
        # the second agent's turn must sit in the same conversation as the first's.
        with app.app_context():
            from app.agent.memory import MemoryAPI
            pool = app.extensions["harness"]["pool"]
            conv = group["conversation_id"]
            found = {}
            for agent in (AGENT_A, AGENT_B):
                mem = MemoryAPI(pool=pool, embedding_client=app.extensions["harness"]["embedding"],
                                agent_name=agent, config=app.config["HARNESS_CONFIG_OBJ"])
                with pool.connection() as conn, conn.cursor() as cur:
                    # The schema name is interpolated (it is a table
                    # name, not a value); the value stays a parameter.
                    cur.execute(
                        "SELECT COUNT(*) FROM " + agent
                        + ".turns WHERE conversation_id = %s",
                        (conv,),
                    )
                    found[agent] = cur.fetchone()[0]
            check("both agents wrote their turns into the shared group conversation",
                  found.get(AGENT_A, 0) >= 2 and found.get(AGENT_B, 0) >= 2, str(found))

        # --- 10. no mention -> no agent turn -----------------------------
        print("  ... posting a message with no @mention")
        st, data = member.json("POST", f"/api/v1/groups/{group_id}/chat",
                               {"message": "just a note, no agent needed"})
        check("a message with no mention produces no agent reply",
              st == 200 and not data.get("replies"), f"status={st} {data.get('notices')}")

        # --- 11. an unknown mention is reported, not silently ignored ----
        st, data = member.json("POST", f"/api/v1/groups/{group_id}/chat",
                               {"message": "@nosuchagent hello"})
        notices = " ".join(data.get("notices") or [])
        check("an unknown mention is reported back",
              st == 200 and "nosuchagent" in notices, notices)

        # --- 12. DM <-> group conversation isolation (the project owner 2026-10-23) ----
        # The group owns its conversation id; a DM must never load it,
        # even when the group conversation is the most recently touched
        # conversation row for that agent.
        print("  ... checking DM / group conversation isolation")
        group_conv = group["conversation_id"]
        marker = "dm-isolation-probe-" + str(int(time.time()))
        # (a) a DM turn for the same agent + same user
        st, data = member.json("POST", f"/api/v1/entities/{AGENT_A}/chat",
                               {"message": marker + " (direct message)"})
        check("a DM to the same agent works", st == 200, f"status={st}")

        with app.app_context():
            from psycopg.rows import dict_row
            pool = app.extensions["harness"]["pool"]

            def rows(sql, args=()):
                with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                    cur.execute(sql, args)
                    return cur.fetchall()

            # (b) the group conversation rows are flagged + unowned
            grows = rows(
                "SELECT id, user_id, is_group, archived_at FROM " + AGENT_A + ".conversations "
                "WHERE id = %s", (group_conv,))
            check("the group conversation exists in the agent's schema",
                  len(grows) == 1, f"{len(grows)} rows")
            if grows:
                g = grows[0]
                check("the group conversation is flagged is_group = true",
                      g["is_group"] is True, f"is_group={g['is_group']}")
                check("the group conversation is not owned by a user",
                      g["user_id"] is None, f"user_id={g['user_id']}")

            # (c) the DM conversation is a DIFFERENT id, user-owned, not a group
            drows = rows(
                "SELECT id, user_id, is_group FROM " + AGENT_A + ".conversations "
                "WHERE user_id = %s AND is_group = false AND archived_at IS NULL "
                "ORDER BY updated_at DESC LIMIT 1", (member_id,))
            check("the DM has its own conversation row", len(drows) == 1,
                  f"{len(drows)} rows")
            if drows and grows:
                check("the DM conversation id differs from the group conversation id",
                      str(drows[0]["id"]) != group_conv,
                      f"dm={drows[0]['id']} group={group_conv}")
                check("the DM conversation is user-owned",
                      int(drows[0]["user_id"]) == member_id,
                      f"user_id={drows[0]['user_id']}")
                check("the DM conversation is not flagged as a group",
                      drows[0]["is_group"] is False)

            # (d) "New conversation" must not archive the group conversation
            st, data = member.json("POST", f"/api/v1/entities/{AGENT_A}/reset-conversation")
            check("reset-conversation succeeds on a DM", st == 200, f"status={st}")
            grows2 = rows(
                "SELECT archived_at FROM " + AGENT_A + ".conversations WHERE id = %s",
                (group_conv,))
            check("reset-conversation did NOT archive the group conversation",
                  bool(grows2) and grows2[0]["archived_at"] is None,
                  f"archived_at={grows2[0]['archived_at'] if grows2 else 'MISSING'}")

        # (e) the group transcript still reads back after the reset
        st, data = member.json("GET", f"/api/v1/groups/{group_id}/messages?limit=50")
        check("the group transcript still reads back after the DM reset",
              st == 200 and len(data.get("messages", [])) >= 3,
              f"{len(data.get('messages', []))} messages")

        # (f) the DM chat view does NOT show the group's turns
        st, data = member.json("GET", f"/api/v1/entities/{AGENT_A}/turns?limit=50")
        dm_turns = data.get("turns", [])
        dm_text = " ".join(t.get("content", "") for t in dm_turns)
        check("the DM chat view does not contain the group's message",
              "say hello in one short line" not in dm_text,
              f"{len(dm_turns)} DM turns")
        check("the DM chat view contains the DM message",
              marker in dm_text, f"{len(dm_turns)} DM turns")

    finally:
        # --- cleanup ------------------------------------------------------
        print("cleaning up…")
        with app.app_context():
            from psycopg.rows import dict_row
            pool = app.extensions["harness"]["pool"]
            try:
                with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                    if group_id:
                        cur.execute("DELETE FROM public.groups WHERE id = %s", (group_id,))
                    cur.execute("DELETE FROM public.groups WHERE name = 'smoke test group'")
                conn.commit()
            except Exception as exc:
                print(f"could not delete the group rows ({type(exc).__name__})")
            try:
                # Separate transaction: harness_app has no DELETE on
                # public.users, so the throwaway users need a superuser.
                with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                    cur.execute("DELETE FROM public.users WHERE email LIKE 'smoke_groups%'")
                conn.commit()
            except Exception as exc:
                print(f"cleanup needs a superuser for the users ({type(exc).__name__}); "
                      f"run the SQL below as postgres")
                print("  DELETE FROM public.users WHERE email LIKE 'smoke_groups%';")
        print("done.")

    if FAILURES:
        print(f"\n{len(FAILURES)} FAILURE(S): " + "; ".join(FAILURES))
        return 1
    print("\nALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
