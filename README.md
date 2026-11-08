# meerkat-mcp

MCP server for [Meerkat CRM](https://github.com/fbuchner/meerkat-crm), built to run in
[Obot](https://obot.ai) as a **containerized** server (Streamable HTTP on `:8080/mcp`,
health on `/health`). Also runs over stdio when `PORT` is unset.

## Configure

| Variable | Notes |
|---|---|
| `MEERKAT_URL` | Required. Base URL of your instance. `/api/v1` is added if missing. Must be reachable from the Obot host. |
| `MEERKAT_API_TOKEN` | Preferred. Sent as `Authorization: Bearer`. |
| `MEERKAT_USERNAME` / `MEERKAT_PASSWORD` | Fallback if no token. Logs in via `/login`, re-logs in on 401. |
| `MEERKAT_READ_ONLY=true` | Registers read tools only. |
| `MEERKAT_ALLOW_DELETE=true` | Also registers `delete_*` tools (off by default; prefer `set_contact_archived`). |

## Run / build

```bash
pip install -r requirements.txt
MEERKAT_URL=https://crm.example.com MEERKAT_API_TOKEN=... python server.py        # stdio
MEERKAT_URL=... MEERKAT_API_TOKEN=... PORT=8080 python server.py                  # http://localhost:8080/mcp

docker build -t ghcr.io/YOUR_ORG/meerkat-mcp:0.1.0 .
docker push ghcr.io/YOUR_ORG/meerkat-mcp:0.1.0      # make the package public: Obot pulls without auth
```

Then in Obot: MCP Servers -> add a Containerized server using `obot-catalog-entry.yaml`
(image, port `8080`, path `/mcp`, and the env vars above, with the token/password marked sensitive).

## Tools

Read: `get_current_user`, `list_contacts`, `get_contact`, `list_circles`, `upcoming_birthdays`,
`random_contacts`, `list_relationships`, `list_notes`, `get_note`, `list_activities`, `get_activity`,
`list_reminders`, `reminder_history`, `network_graph`.

Write: `create_contact`, `update_contact`, `set_contact_archived`, `create_relationship`, `add_note`,
`update_note`, `log_activity`, `update_activity`, `create_reminder`, `update_reminder`, `complete_reminder`.

Delete (opt-in): `delete_contact`, `delete_note`, `delete_activity`, `delete_reminder`.

Update tools read the record first and merge your changes into it, so a partial update can't blank other fields.

## What is and isn't verified

Automated: `python tests/test_e2e.py` runs the server against a **mock** Meerkat API with a real MCP client over
Streamable HTTP (auth modes, re-login, read-only and delete gating, error parsing, and regression tests for the
Meerkat quirks below).

Live-tested against a real Meerkat (v1.5-style data model, behind a reverse proxy):

- Working: `get_current_user`, `list_contacts` (paging, `search`, `circle`), `list_circles`, `upcoming_birthdays`,
  `random_contacts`, `list_reminders` (upcoming), `list_notes`, `list_activities` (all, per contact, `search`),
  `list_relationships`, `network_graph`, `update_contact` (partial update preserves other fields),
  `add_note`, `update_note`, `log_activity`, `update_activity` (linked contacts preserved),
  `set_contact_archived` (archive and unarchive).
- Meerkat quirks the server now handles: dates must be RFC3339 (bare `YYYY-MM-DD` is converted); notes require a date
  (defaults to now); reminders require `contact_id` in the body and a `recurrence`; relationships store the related
  person's *name* in `name` and the relationship in `type`; errors are nested under `"error"` with the bad field in
  `details`.
- Activity date filtering is done in the server (Meerkat's own parameter names for it aren't known), so
  `date_from` / `date_to` work but fetch all activities first.
- `list_activities` with `search` reports an inflated `total` from Meerkat (it counts joined rows); the server
  returns its own `count`.

Still unverified against a real instance after the fixes: `create_reminder` (one-off reminders send
`recurrence: "once"`, which is a guess - if Meerkat rejects it the error will list the allowed values),
`update_reminder`, `complete_reminder`, `reminder_history`, `create_relationship`, `create_contact`, and the
`delete_*` tools.

If you see intermittent `403 Forbidden` on the MCP call itself (the message says the POST to the endpoint was blocked),
that is coming from a proxy/firewall in front of the MCP server, not from this code. Retrying works.
