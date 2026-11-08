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

Tested end-to-end (`python tests/test_e2e.py`) against a **mock** Meerkat API using a real MCP client over
Streamable HTTP: auth modes, re-login, read-only and delete gating, error surfacing, non-localhost Host header.

Not tested against a real Meerkat instance. Endpoints come from Meerkat's published API reference, but it
doesn't document request bodies, so these are best guesses to check on first use:

- Query params on `GET /contacts` (`search`, `circle`, `archived`, `page`, `limit`) and `GET /activities`
  (`search`, `date_from`, `date_to`).
- Body field names for activities (`contact_ids`), reminders (`remind_at`, `by_mail`, `recurrence`,
  `reoccur_from_completion`), notes (`content`) and relationships (`related_contact_id`, `type`).
- Contact fields are passed through as a free-form `fields` object, so `get_contact` on an existing
  record shows the exact names for your Meerkat version (v1.5 added multi-value fields like `emails`).
- Whether API tokens are accepted as Bearer on your version.

Wrong guesses show up as a clear `VALIDATION_ERROR` from Meerkat, and are a one-line fix in `server.py`.
