"""MCP server for Meerkat CRM (https://github.com/fbuchner/meerkat-crm).

Transport: Streamable HTTP at /mcp (what Obot's `containerized` runtime expects),
or stdio when no PORT is set.

Configuration (environment):
  MEERKAT_URL            Base URL of your Meerkat instance, e.g. https://crm.example.com
                         ("/api/v1" is appended automatically if missing).
  MEERKAT_API_TOKEN      Preferred: a Meerkat API token (sent as a Bearer token).
  MEERKAT_USERNAME /     Fallback: log in with username + password. The session is
  MEERKAT_PASSWORD       refreshed automatically on 401.
  MEERKAT_READ_ONLY      "true" -> register read tools only.
  MEERKAT_ALLOW_DELETE   "true" -> also register the delete_* tools (off by default).
  PORT / HOST            HTTP listen port (default 8080 when PORT is set) / host.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from starlette.requests import Request
from starlette.responses import JSONResponse

log = logging.getLogger("meerkat-mcp")

# --------------------------------------------------------------------------- config


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _api_base(url: str) -> str:
    url = url.strip().rstrip("/")
    return url if url.endswith("/api/v1") else url + "/api/v1"


READ_ONLY = _flag("MEERKAT_READ_ONLY")
ALLOW_DELETE = _flag("MEERKAT_ALLOW_DELETE") and not READ_ONLY


# --------------------------------------------------------------------------- client


class MeerkatClient:
    """Thin async client. Bearer token if given, else cookie session via /login."""

    def __init__(self) -> None:
        raw = os.environ.get("MEERKAT_URL", "").strip()
        if not raw:
            raise SystemExit("MEERKAT_URL is required (e.g. https://crm.example.com)")
        self.base = _api_base(raw)
        self.token = os.environ.get("MEERKAT_API_TOKEN", "").strip()
        self.username = os.environ.get("MEERKAT_USERNAME", "").strip()
        self.password = os.environ.get("MEERKAT_PASSWORD", "")
        if not self.token and not (self.username and self.password):
            raise SystemExit(
                "Set MEERKAT_API_TOKEN, or MEERKAT_USERNAME and MEERKAT_PASSWORD"
            )
        self._http = httpx.AsyncClient(timeout=30.0, follow_redirects=False)
        self._logged_in = False

    async def _login(self) -> None:
        r = await self._http.post(
            f"{self.base}/login",
            json={"username": self.username, "password": self.password},
        )
        if r.status_code >= 400:
            raise ToolError(f"Meerkat login failed ({r.status_code}): {_err_text(r)}")
        self._logged_in = True
        # If the instance also returns the JWT in the body, prefer it as a Bearer
        # token (works even if the cookie is marked Secure and we are on plain HTTP).
        try:
            body = r.json()
            tok = body.get("token") if isinstance(body, dict) else None
            if tok:
                self.token = tok
        except ValueError:
            pass

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
    ) -> Any:
        if not self.token and not self._logged_in:
            await self._login()
        params = {k: v for k, v in (params or {}).items() if v is not None}

        async def once() -> httpx.Response:
            try:
                return await self._http.request(
                    method,
                    f"{self.base}{path}",
                    params=params,
                    json=json_body,
                    headers=self._headers(),
                )
            except httpx.HTTPError as e:
                raise ToolError(f"Cannot reach Meerkat at {self.base}: {e}") from e

        r = await once()
        if r.status_code == 401 and self.username and self.password:
            self.token = os.environ.get("MEERKAT_API_TOKEN", "").strip()
            self._logged_in = False
            await self._login()
            r = await once()
        if r.status_code >= 400:
            raise ToolError(f"Meerkat {method} {path} -> {r.status_code}: {_err_text(r)}")
        if r.status_code == 204 or not r.content:
            return {"ok": True}
        try:
            return r.json()
        except ValueError:
            return {"ok": True, "body": r.text[:2000]}


def _err_text(r: httpx.Response) -> str:
    """Meerkat errors look like {"error": {"code", "message", "details": {...}}}.
    The details name the offending field, so keep them."""
    try:
        b = r.json()
    except ValueError:
        return r.text[:500]
    if isinstance(b, dict):
        e = b.get("error") if isinstance(b.get("error"), dict) else b
        if isinstance(e, dict) and ("message" in e or "code" in e):
            text = f"{e.get('code', '')} {e.get('message', '')}".strip()
            if e.get("details"):
                text += " | details: " + json.dumps(e["details"], ensure_ascii=False)
            return text[:800]
    return r.text[:500]


def _rfc3339(value: str) -> str:
    """Meerkat parses dates as RFC3339 and rejects bare YYYY-MM-DD."""
    v = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
        return v + "T00:00:00Z"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?(\.\d+)?", v):
        if len(v) == 16:
            v += ":00"
        return v + "Z"
    return v


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


_client: MeerkatClient | None = None


def client() -> MeerkatClient:
    global _client
    if _client is None:
        _client = MeerkatClient()
    return _client


def _unwrap(data: Any, *keys: str) -> Any:
    """Meerkat list endpoints may return a bare list or {"<name>": [...], ...}."""
    if isinstance(data, dict):
        for k in keys:
            if k in data:
                return data[k]
    return data


def _out(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=1, default=str)


async def _merge_update(path: str, fields: dict[str, Any], *keys: str) -> Any:
    """Read-modify-write so a partial update from an LLM never blanks other fields."""
    cur = await client().request("GET", path)
    cur = _unwrap(cur, *keys)
    if not isinstance(cur, dict):
        raise ToolError(f"Unexpected response shape from GET {path}")
    merged = {**cur, **fields}
    return await client().request("PUT", path, json_body=merged)


# --------------------------------------------------------------------------- server

mcp = FastMCP(
    "meerkat-crm",
    instructions=(
        "Tools for Meerkat CRM, a self-hosted personal CRM (contacts, circles, "
        "relationships, notes, activities, reminders, birthdays). Look contacts up "
        "with list_contacts before writing; ids are numeric. Dates are ISO 8601 "
        "(YYYY-MM-DD for dates, full timestamps for reminders)."
        + (" This server is READ-ONLY." if READ_ONLY else "")
    ),
    host=os.environ.get("HOST", "0.0.0.0"),
    port=int(os.environ.get("PORT", "8080")),
    stateless_http=True,
    json_response=True,
)

RO = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
DESTROY = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> JSONResponse:
    return JSONResponse({"status": "healthy", "service": "meerkat-mcp"})


def write_tool(**kw):
    """Register only when not read-only."""
    def deco(fn):
        return mcp.tool(annotations=kw.pop("annotations", WRITE), **kw)(fn) if not READ_ONLY else fn
    return deco


def delete_tool(**kw):
    def deco(fn):
        return mcp.tool(annotations=DESTROY, **kw)(fn) if ALLOW_DELETE else fn
    return deco


# ---- reads -----------------------------------------------------------------------


@mcp.tool(annotations=RO)
async def get_current_user() -> str:
    """Return the Meerkat user this server is authenticated as (also a connectivity check)."""
    return _out(await client().request("GET", "/users/me"))


@mcp.tool(annotations=RO)
async def list_contacts(
    search: str | None = None,
    circle: str | None = None,
    include_archived: bool = False,
    page: int = 1,
    limit: int = 50,
) -> str:
    """List or search contacts. `search` matches names and other text; `circle` filters to one circle."""
    data = await client().request(
        "GET",
        "/contacts",
        params={
            "search": search,
            "circle": circle,
            "archived": "true" if include_archived else None,
            "page": page,
            "limit": limit,
        },
    )
    return _out(data)


@mcp.tool(annotations=RO)
async def get_contact(contact_id: int) -> str:
    """Get one contact with its full details."""
    return _out(await client().request("GET", f"/contacts/{contact_id}"))


@mcp.tool(annotations=RO)
async def list_circles() -> str:
    """List all circles (groups such as Family, Friends, Work) currently in use."""
    return _out(await client().request("GET", "/contacts/circles"))


@mcp.tool(annotations=RO)
async def upcoming_birthdays() -> str:
    """Upcoming birthdays across all contacts."""
    return _out(await client().request("GET", "/contacts/birthdays"))


@mcp.tool(annotations=RO)
async def random_contacts() -> str:
    """Five random contacts - handy for 'who should I get in touch with?'."""
    return _out(await client().request("GET", "/contacts/random"))


@mcp.tool(annotations=RO)
async def list_relationships(contact_id: int) -> str:
    """Relationships of a contact, both outgoing ('Sister: Eva') and incoming ('Sister of Jon')."""
    out = await client().request("GET", f"/contacts/{contact_id}/relationships")
    inc = await client().request("GET", f"/contacts/{contact_id}/incoming-relationships")
    return _out({"outgoing": out, "incoming": inc})


@mcp.tool(annotations=RO)
async def list_notes(contact_id: int | None = None) -> str:
    """Notes for one contact, or (without contact_id) the general notes not attached to anyone."""
    path = f"/contacts/{contact_id}/notes" if contact_id is not None else "/notes"
    return _out(await client().request("GET", path))


@mcp.tool(annotations=RO)
async def get_note(note_id: int) -> str:
    """Get a single note."""
    return _out(await client().request("GET", f"/notes/{note_id}"))


@mcp.tool(annotations=RO)
async def list_activities(
    contact_id: int | None = None,
    search: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> str:
    """Activities (meetings, calls, dinners). Per contact, or across everyone. `search` is matched by Meerkat;
    `date_from` / `date_to` (YYYY-MM-DD, inclusive) are applied here, because Meerkat's own date parameters
    aren't known. Results include linked contacts for the per-contact view."""
    path = f"/contacts/{contact_id}/activities" if contact_id is not None else "/activities"
    items: list[dict[str, Any]] = []
    seen: set[Any] = set()
    for page in range(1, 21):
        data = await client().request("GET", path, params={"search": search, "page": page, "limit": 100})
        batch = _unwrap(data, "activities")
        if not isinstance(batch, list) or not batch:
            break
        size = data.get("limit") if isinstance(data, dict) and data.get("limit") else 100
        fresh = [a for a in batch if isinstance(a, dict) and a.get("ID") not in seen]
        if not fresh:  # endpoint ignores paging and repeated itself
            break
        seen.update(a.get("ID") for a in fresh)
        items.extend(fresh)
        if len(batch) < size:
            break
    lo = date_from[:10] if date_from else None
    hi = date_to[:10] if date_to else None
    items = [
        a for a in items
        if (not lo or str(a.get("date", ""))[:10] >= lo) and (not hi or str(a.get("date", ""))[:10] <= hi)
    ]
    return _out({"activities": items, "count": len(items)})


@mcp.tool(annotations=RO)
async def get_activity(activity_id: int) -> str:
    """Get a single activity."""
    return _out(await client().request("GET", f"/activities/{activity_id}"))


@mcp.tool(annotations=RO)
async def list_reminders(contact_id: int | None = None, upcoming_only: bool = False) -> str:
    """Reminders: all, only upcoming/overdue ones (dashboard view), or those of one contact."""
    if contact_id is not None:
        path = f"/contacts/{contact_id}/reminders"
    elif upcoming_only:
        path = "/reminders/upcoming"
    else:
        path = "/reminders"
    return _out(await client().request("GET", path))


@mcp.tool(annotations=RO)
async def reminder_history(contact_id: int) -> str:
    """Completed-reminder timeline entries for a contact (when you last kept in touch)."""
    return _out(await client().request("GET", f"/contacts/{contact_id}/reminder-completions"))


@mcp.tool(annotations=RO)
async def network_graph() -> str:
    """Compact summary of the contact network: relationships as 'A --Type--> B' and which contacts shared
    each activity. (Meerkat's raw graph is ~250 nodes, so this returns names, not node lists.)"""
    data = await client().request("GET", "/graph")
    nodes = {n.get("id"): n for n in data.get("nodes", [])} if isinstance(data, dict) else {}

    def label(i: Any) -> str:
        return str(nodes.get(i, {}).get("label", i))

    relationships: list[str] = []
    activities: dict[str, list[str]] = {}
    for e in data.get("edges", []) if isinstance(data, dict) else []:
        if e.get("type") == "relationship":
            relationships.append(f"{label(e.get('source'))} --{e.get('label', '')}--> {label(e.get('target'))}")
        elif e.get("type") == "activity":
            activities.setdefault(label(e.get("source")), []).append(label(e.get("target")))
    contact_count = sum(1 for n in nodes.values() if n.get("type") == "contact")
    return _out({"contacts": contact_count, "relationships": relationships, "activities": activities})


# ---- writes ----------------------------------------------------------------------


@write_tool()
async def create_contact(fields: dict[str, Any]) -> str:
    """Create a contact. `fields` is the contact JSON, e.g.
    {"firstname": "Eva", "lastname": "Smith", "circles": ["Family"], "birthday": "1990-04-12",
     "email": "eva@example.com", "how_we_met": "...", "food_preference": "vegetarian"}.
    Only firstname is normally required. Check get_contact on an existing record for the exact field names."""
    return _out(await client().request("POST", "/contacts", json_body=fields))


@write_tool()
async def update_contact(contact_id: int, fields: dict[str, Any]) -> str:
    """Update a contact. Only the keys in `fields` change; everything else is preserved."""
    return _out(await _merge_update(f"/contacts/{contact_id}", fields, "contact"))


@write_tool()
async def set_contact_archived(contact_id: int, archived: bool) -> str:
    """Archive or unarchive a contact. NOTE: archiving permanently deletes that contact's active reminders."""
    action = "archive" if archived else "unarchive"
    return _out(await client().request("POST", f"/contacts/{contact_id}/{action}"))


@write_tool()
async def create_relationship(
    contact_id: int, related_contact_id: int, relationship_type: str
) -> str:
    """Link two contacts, e.g. contact 3 --'Sister'--> contact 9 (shown as 'Sister: <name>')."""
    # In Meerkat's data, `name` is the related person's name and `type` is the relationship
    # ("Spouse"), so look the person up rather than guessing.
    other = _unwrap(await client().request("GET", f"/contacts/{related_contact_id}"), "contact")
    name = " ".join(
        p for p in (other.get("firstname"), other.get("lastname")) if p
    ) if isinstance(other, dict) else ""
    body = {
        "contact_id": contact_id,
        "related_contact_id": related_contact_id,
        "type": relationship_type,
        "name": name,
    }
    return _out(await client().request("POST", f"/contacts/{contact_id}/relationships", json_body=body))


@write_tool()
async def add_note(content: str, contact_id: int | None = None, date: str | None = None) -> str:
    """Add a note to a contact's timeline, or a general journal note if contact_id is omitted.
    `date` defaults to now (Meerkat requires one); date-only values are accepted."""
    body: dict[str, Any] = {"content": content, "date": _rfc3339(date) if date else _now()}
    path = f"/contacts/{contact_id}/notes" if contact_id is not None else "/notes"
    return _out(await client().request("POST", path, json_body=body))


@write_tool()
async def update_note(note_id: int, fields: dict[str, Any]) -> str:
    """Update a note (e.g. {"content": "..."}); other fields are preserved."""
    return _out(await _merge_update(f"/notes/{note_id}", fields, "note"))


@write_tool()
async def log_activity(
    title: str,
    date: str,
    contact_ids: list[int],
    description: str | None = None,
    location: str | None = None,
) -> str:
    """Log an activity (dinner, call, meeting) with one or more contacts. Two or more contacts puts it on the network graph."""
    body: dict[str, Any] = {
        "title": title,
        "date": _rfc3339(date),
        "contact_ids": contact_ids,
    }
    if description:
        body["description"] = description
    if location:
        body["location"] = location
    return _out(await client().request("POST", "/activities", json_body=body))


@write_tool()
async def update_activity(activity_id: int, fields: dict[str, Any]) -> str:
    """Update an activity; only the keys in `fields` change."""
    return _out(await _merge_update(f"/activities/{activity_id}", fields, "activity"))


@write_tool()
async def create_reminder(
    contact_id: int,
    message: str,
    remind_at: str,
    recurrence: str | None = None,
    by_mail: bool | None = None,
    reoccur_from_completion: bool | None = None,
) -> str:
    """Create a reminder for a contact. `remind_at` is an ISO timestamp (date-only is accepted).
    `recurrence` e.g. 'weekly', 'monthly'; Meerkat requires a value, so one-off reminders send 'once'
    (if Meerkat rejects that, the error names the allowed values). `reoccur_from_completion` reschedules
    from the day it is completed rather than the due date."""
    body: dict[str, Any] = {
        "contact_id": contact_id,  # Meerkat requires it in the body as well as the path
        "message": message,
        "remind_at": _rfc3339(remind_at),
        "recurrence": recurrence or "once",
    }
    if by_mail is not None:
        body["by_mail"] = by_mail
    if reoccur_from_completion is not None:
        body["reoccur_from_completion"] = reoccur_from_completion
    return _out(await client().request("POST", f"/contacts/{contact_id}/reminders", json_body=body))


@write_tool()
async def update_reminder(reminder_id: int, fields: dict[str, Any]) -> str:
    """Update a reminder; only the keys in `fields` change."""
    return _out(await _merge_update(f"/reminders/{reminder_id}", fields, "reminder"))


@write_tool()
async def complete_reminder(reminder_id: int) -> str:
    """Mark a reminder done. Recurring reminders reschedule; a timeline entry is recorded."""
    return _out(await client().request("POST", f"/reminders/{reminder_id}/complete"))


# ---- deletes (opt-in) ------------------------------------------------------------


@delete_tool()
async def delete_contact(contact_id: int) -> str:
    """PERMANENTLY delete a contact. Prefer set_contact_archived unless the user explicitly wants deletion."""
    return _out(await client().request("DELETE", f"/contacts/{contact_id}"))


@delete_tool()
async def delete_note(note_id: int) -> str:
    """Permanently delete a note."""
    return _out(await client().request("DELETE", f"/notes/{note_id}"))


@delete_tool()
async def delete_activity(activity_id: int) -> str:
    """Permanently delete an activity."""
    return _out(await client().request("DELETE", f"/activities/{activity_id}"))


@delete_tool()
async def delete_reminder(reminder_id: int) -> str:
    """Permanently delete a reminder."""
    return _out(await client().request("DELETE", f"/reminders/{reminder_id}"))


# --------------------------------------------------------------------------- main


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    client()  # fail fast on bad config
    if os.environ.get("PORT"):
        log.info("Serving Streamable HTTP on %s:%s/mcp", mcp.settings.host, mcp.settings.port)
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
