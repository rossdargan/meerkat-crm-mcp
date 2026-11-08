"""End-to-end test: mock Meerkat API <- meerkat-mcp (HTTP, /mcp) <- real MCP client.

Run:  python tests/test_e2e.py
"""
import asyncio
import json
import os
import subprocess
import sys
import time

import httpx
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "..", "server.py")

# ------------------------------------------------------------------ mock Meerkat
STATE = {"contacts": {1: {"id": 1, "firstname": "Eva", "lastname": "Smith",
                          "circles": ["Family"], "email": "eva@x.org", "birthday": "1990-04-12"}},
         "next": 2, "logins": 0, "last_put": None, "last_params": None}
VALID = {"cookie": "sess-1"}


def authed(req: Request) -> bool:
    if req.headers.get("authorization") == "Bearer good-token":
        return True
    return req.cookies.get("auth") == VALID["cookie"]


def err(code, status, msg):
    return JSONResponse({"code": code, "message": msg, "details": {}}, status_code=status)


async def login(req: Request):
    body = await req.json()
    if body != {"username": "ross", "password": "pw"}:
        return err("INVALID_CREDENTIALS", 401, "bad creds")
    STATE["logins"] += 1
    VALID["cookie"] = f"sess-{STATE['logins'] + 1}"
    r = JSONResponse({"message": "ok"})
    r.set_cookie("auth", VALID["cookie"], httponly=True)
    return r


async def me(req):
    return JSONResponse({"id": 7, "username": "ross"}) if authed(req) else err("UNAUTHORIZED", 401, "no")


async def contacts(req: Request):
    if not authed(req):
        return err("UNAUTHORIZED", 401, "no")
    if req.method == "POST":
        b = await req.json()
        cid = STATE["next"]; STATE["next"] += 1
        STATE["contacts"][cid] = {"id": cid, **b}
        return JSONResponse(STATE["contacts"][cid], status_code=201)
    STATE["last_params"] = dict(req.query_params)
    return JSONResponse({"contacts": list(STATE["contacts"].values()), "total": len(STATE["contacts"])})


async def contact(req: Request):
    if not authed(req):
        return err("UNAUTHORIZED", 401, "no")
    cid = int(req.path_params["id"])
    if cid not in STATE["contacts"]:
        return err("NOT_FOUND", 404, "Contact not found")
    if req.method == "PUT":
        STATE["last_put"] = await req.json()
        STATE["contacts"][cid] = STATE["last_put"]
        return JSONResponse(STATE["contacts"][cid])
    if req.method == "DELETE":
        del STATE["contacts"][cid]
        return Response(status_code=204)
    return JSONResponse(STATE["contacts"][cid])


async def health(_):
    return JSONResponse({"status": "ok"})


mock = Starlette(routes=[
    Route("/api/v1/login", login, methods=["POST"]),
    Route("/api/v1/users/me", me),
    Route("/api/v1/contacts", contacts, methods=["GET", "POST"]),
    Route("/api/v1/contacts/{id:int}", contact, methods=["GET", "PUT", "DELETE"]),
    Route("/health", health),
])


def serve_mock(port):
    cfg = uvicorn.Config(mock, host="127.0.0.1", port=port, log_level="error")
    srv = uvicorn.Server(cfg)
    t = asyncio.get_event_loop().create_task(srv.serve())
    return srv, t


# ------------------------------------------------------------------ helpers
def start_server(port, mock_port, **env):
    e = {**os.environ, "MEERKAT_URL": f"http://127.0.0.1:{mock_port}", "PORT": str(port),
         "MEERKAT_API_TOKEN": "", "MEERKAT_USERNAME": "", "MEERKAT_PASSWORD": "",
         "MEERKAT_READ_ONLY": "", "MEERKAT_ALLOW_DELETE": "", **env}
    p = subprocess.Popen([sys.executable, SERVER], env=e, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    for _ in range(60):
        try:
            if httpx.get(f"http://127.0.0.1:{port}/health", timeout=0.5).status_code == 200:
                return p
        except Exception:
            time.sleep(0.2)
    p.terminate()
    raise RuntimeError("server did not start: " + p.stdout.read().decode())


async def call(session, name, **args):
    r = await session.call_tool(name, args)
    text = r.content[0].text if r.content else ""
    return r.isError, text


async def with_session(port, fn, host_header=None):
    headers = {"Host": host_header} if host_header else None
    async with streamablehttp_client(f"http://127.0.0.1:{port}/mcp", headers=headers) as (rd, wr, _):
        async with ClientSession(rd, wr) as s:
            await s.initialize()
            return await fn(s)


def check(cond, label):
    print(("PASS " if cond else "FAIL ") + label)
    if not cond:
        check.failed = True
check.failed = False


async def main():
    mock_port, port = 18901, 18902
    srv, task = serve_mock(mock_port)
    await asyncio.sleep(0.5)
    procs = []
    try:
        # ---- 1. token auth, full tool set, deletes OFF by default
        p = start_server(port, mock_port, MEERKAT_API_TOKEN="good-token"); procs.append(p)

        async def scenario1(s):
            names = {t.name for t in (await s.list_tools()).tools}
            check("list_contacts" in names and "update_contact" in names, "tools listed over streamable HTTP")
            check("delete_contact" not in names, "delete tools hidden by default")
            tools = {t.name: t for t in (await s.list_tools()).tools}
            check(tools["list_contacts"].annotations.readOnlyHint is True, "read tools annotated readOnly")
            e, t = await call(s, "get_current_user"); check(not e and "ross" in t, "bearer token auth works")
            e, t = await call(s, "list_contacts", search="eva", circle="Family")
            check(not e and "Eva" in t and STATE["last_params"].get("search") == "eva"
                  and STATE["last_params"].get("circle") == "Family", "list_contacts passes search+circle")
            e, t = await call(s, "update_contact", contact_id=1, fields={"email": "new@x.org"})
            check(not e and STATE["last_put"]["firstname"] == "Eva" and STATE["last_put"]["email"] == "new@x.org",
                  "update_contact merges (other fields preserved)")
            e, t = await call(s, "get_contact", contact_id=99)
            check(e and "NOT_FOUND" in t, "API errors surface as tool errors")
            e, t = await call(s, "create_contact", fields={"firstname": "Zed"}); check(not e and "Zed" in t, "create_contact")
        await with_session(port, scenario1)
        # non-localhost Host header (as when Obot proxies via a container hostname)
        async def ping(s):
            return len((await s.list_tools()).tools)
        n = await with_session(port, ping, host_header="meerkat-mcp:8080")
        check(n > 0, "accepts non-localhost Host header (gateway/container use)")
        p.terminate(); p.wait(timeout=10); procs.remove(p)

        # ---- 2. password login + auto re-login on expired session
        p = start_server(port, mock_port, MEERKAT_USERNAME="ross", MEERKAT_PASSWORD="pw"); procs.append(p)

        async def scenario2(s):
            e, t = await call(s, "get_current_user"); check(not e and "ross" in t, "password login works")
            VALID["cookie"] = "rotated-server-side"      # invalidate session
            e, t = await call(s, "get_current_user"); check(not e and "ross" in t, "re-logs in after 401")
            check(STATE["logins"] == 2, "exactly one extra login performed")
        await with_session(port, scenario2)
        p.terminate(); p.wait(timeout=10); procs.remove(p)

        # ---- 3. read-only mode and delete opt-in
        p = start_server(port, mock_port, MEERKAT_API_TOKEN="good-token", MEERKAT_READ_ONLY="true"); procs.append(p)
        async def scenario3(s):
            names = {t.name for t in (await s.list_tools()).tools}
            check("list_contacts" in names and not ({"create_contact", "update_contact", "log_activity",
                  "complete_reminder", "delete_contact"} & names), "read-only exposes no write/delete tools")
        await with_session(port, scenario3)
        p.terminate(); p.wait(timeout=10); procs.remove(p)

        p = start_server(port, mock_port, MEERKAT_API_TOKEN="good-token", MEERKAT_ALLOW_DELETE="true"); procs.append(p)
        async def scenario4(s):
            names = {t.name for t in (await s.list_tools()).tools}
            check("delete_contact" in names, "MEERKAT_ALLOW_DELETE exposes delete tools")
            e, t = await call(s, "delete_contact", contact_id=2); check(not e and 2 not in STATE["contacts"], "delete_contact works")
        await with_session(port, scenario4)
        p.terminate(); p.wait(timeout=10); procs.remove(p)

        # ---- 4. bad credentials fail clearly
        p = start_server(port, mock_port, MEERKAT_USERNAME="ross", MEERKAT_PASSWORD="wrong"); procs.append(p)
        async def scenario5(s):
            e, t = await call(s, "get_current_user")
            check(e and "login failed" in t.lower(), "bad credentials -> clear error")
        await with_session(port, scenario5)
    finally:
        for p in procs:
            p.terminate()
        srv.should_exit = True
        await task
    print("\nRESULT:", "FAILED" if check.failed else "ALL PASSED")
    sys.exit(1 if check.failed else 0)


asyncio.run(main())
