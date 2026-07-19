# zoho-mcp

A vendor-agnostic MCP (Model Context Protocol) server that exposes Zoho Mail, Zoho Calendar, and Zoho Contacts as tools to any MCP-compatible LLM client (Claude, ChatGPT, Gemini, etc.).

## Status

Phase 1: personal prototype (local stdio transport, single-user Zoho OAuth, read-only tools).

Tools implemented:
- `search_emails` (optionally filtered by `days_back`, resolved via a live, per-process-cached lookup of the mailbox's own timezone -- never assumed or stored statically, since that setting can change; each result includes a `read` bool, confirmed against Zoho's `status` field empirically rather than from docs; excludes Sent/Drafts/Templates by default, matched by `folderType` so rule-filed custom folders are never accidentally caught -- pass an explicit `in:Sent` etc. qualifier to search one of those specifically), `get_email`
- `list_events`
- `search_contacts` (matches name, email, *and* phone number server-side; searches both the Personal and Organization contact pools and merges the results, returning `{"contacts": [...], "has_more": bool}` -- `has_more` is Zoho's own signal, not inferred from a suspiciously-round result count; excludes Archived/Inactive contacts by default -- pass `status="archived"` or `status="inactive"` to search those folders instead, via `filter_type`, an undocumented Zoho param found by inspecting the real Contacts web client's network requests and confirmed against a real archived contact), `get_contact` (requires both `contact_id` and `scope`, since the same id can mean an unrelated record in the other scope -- confirmed live: fetching an org contact's id through the personal endpoint returns a 200 with a different, partial record rather than a 404), `count_contacts` (returns `{"personal": {"contacts": int, "archived": int, "inactive": int}, "organization": {...same shape...}, "total": int}` -- archived/inactive are surfaced as their own properties rather than hidden, though `total` only sums the active `contacts` count from each scope; a dedicated count lookup, no pagination/summing needed) -- Zoho Contacts, a separate Zoho product (`contacts.zoho.com`) from Mail/Calendar, sharing the same OAuth token. Each contact includes a `scope` field plus phones, notes, nickname, and birthday when set.

## Setup

1. Register a Server-based Application in the [Zoho API Console](https://accounts.zoho.com/developerconsole) with scopes `ZohoMail.messages.READ`, `ZohoMail.accounts.READ`, `ZohoMail.folders.READ`, `ZohoCalendar.event.READ`, `ZohoCalendar.calendar.READ`, and `zohocontacts.contactapi.READ` (`.accounts.READ` and `.folders.READ` are also used at runtime, for `search_emails`'s timezone lookup and Sent/Drafts/Templates filtering respectively, not just at setup; `.calendar.READ` is only needed once, to look up the calendar UID in step 4), redirect URI `http://localhost:8765/callback` (change the port via `ZOHO_OAUTH_CALLBACK_PORT` if 8765 is taken).
2. Copy `.env.example` to `.env` and fill in `ZOHO_CLIENT_ID`, `ZOHO_CLIENT_SECRET`.
3. `uv run zoho-mcp-setup` -- opens your browser to approve access, stores a refresh token in the OS credential store (Windows Credential Manager via `keyring`), and prints your `ZOHO_ACCOUNT_ID`/`ZOHO_CALENDAR_UID` values (looked up automatically from your default mail account and calendar). Add both to `.env`.
4. `uv run zoho-mcp` to start the server over stdio.

Optional: set `ZOHO_STRIP_INVISIBLE_CHARS=true` in `.env` to have `get_email` strip invisible Unicode padding characters some marketing emails use to pad preview text. Off by default; never strips zero-width joiner/non-joiner, which carry real meaning in emoji sequences and some scripts.

## Development

```
uv sync
uv run pytest
```

See [CLAUDE.md](CLAUDE.md) for the project's development rules (TDD, layered architecture, error handling, documentation).
