# zoho-mcp

A vendor-agnostic MCP (Model Context Protocol) server that exposes Zoho Mail, Zoho Calendar, and Zoho Contacts as tools to any MCP-compatible LLM client (Claude, ChatGPT, Gemini, etc.).

## Status

Phase 1: personal prototype (local stdio transport, single-user Zoho OAuth, read-only tools).

Tools implemented:
- `search_emails` (optionally filtered by `days_back`, resolved via a live, per-process-cached lookup of the mailbox's own timezone -- never assumed or stored statically, since that setting can change; each result includes a `read` bool, confirmed against Zoho's `status` field empirically rather than from docs; excludes Sent/Drafts/Templates by default, matched by `folderType` so rule-filed custom folders are never accidentally caught -- pass an explicit `in:Sent` etc. qualifier to search one of those specifically; `subject`/`snippet` are HTML-entity-decoded, since Zoho's search API frequently returns literal undecoded entities like `&#39;` -- confirmed live in ~10%/~36% of a real sample respectively; Zoho Mail auto-files certain emails into a real "Notification" folder and also tags them with a matching label -- both `in:Notification` and `label:Notification` work), `get_email`
- `list_attachments` -- metadata only (id, name, size_bytes) for one email's attachments; reading actual attachment content is out of scope (no document-parsing infrastructure to make binary content usefully consumable)
- `list_folders` -- id, name, path (e.g. "/Inbox/Work" -- the real hierarchy signal), type. `previousFolderId` in Zoho's raw response is NOT a parent reference despite the name -- it's a display-order "previous sibling" pointer (a linked list), so it's excluded from the normalized shape
- `list_labels` -- id, name, color for every label/tag configured in the mailbox
- `list_signatures` -- id, name, content for every configured email signature; `content` is HTML-stripped to plain text like `get_email`
- `list_events`, `get_event` (organizer, full attendee list, location, description, recurrence rule -- `list_events` can report only your own attendee entry for a recurring occurrence, not every invitee, which is why `get_event` exists; deliberately excludes start/end since Zoho's single-event endpoint can return the wrong dates for a recurring event -- keep using `list_events`' own start/end for timing; both take an optional `calendar_id` to target a non-default calendar), `list_calendars` (id, name, is_default, timezone, privilege)
- `get_freebusy` -- busy time slots for a given email's calendar. Only returns data for calendars with Zoho Calendar's per-calendar "include in my Free/Busy sharing" setting enabled (Settings → Calendar → My Calendars → a calendar's Details tab) -- raises a clear error rather than silently returning an empty (misreadable as "fully free") list when that isn't the case. Resource free/busy (`/resources/<id>/freebusy`) consistently 404s regardless of which resource identifier is tried and isn't exposed -- unclear whether it needs additional account-level setup.
- `list_tasks`, `get_task` -- Zoho Mail's own Tasks feature (`mail.zoho.com/api/tasks`, same OAuth token family as Mail/Calendar, no separate scope family unlike Contacts). Returns id, title, description, status, priority, due_date, project, assignee, tags, subtask_count, recurring, created_at/modified_at. `created_at`/`modified_at` are already proper ISO 8601 with a real UTC offset straight from Zoho, unlike Mail/Calendar's other timestamp formats. `due_date`'s real format is unverified (never seen populated on this account) -- treated as an opaque string.
- `list_notes`, `get_note` -- Zoho Mail's own Notes feature (`mail.zoho.com/api/notes`). Returns id, title, content, book, owner, is_favorite, color, created_at/modified_at. Unlike Tasks, Notes' timestamps are epoch-millisecond strings (converted like Mail's) rather than pre-formatted ISO 8601. No `has_more` -- Zoho's response includes no paging/total signal for this endpoint at all; fewer results than `limit` is the only sign you've reached the end.
- `list_bookmarks`, `get_bookmark` -- Zoho Mail's own Bookmarks feature (`mail.zoho.com/api/links`). Returns id, title, url, summary, collection, owner, is_favorite, tags. No timestamps at all (bookmarks don't have created/modified fields), and no `has_more` (same as Notes). `is_favorite` is normalized from a string `"true"`/`"false"` on this endpoint -- confirmed live it's a real boolean on the otherwise-identical Notes endpoint, so don't assume type consistency across same-named fields on sibling endpoints.
- `list_branches`, `list_resources` -- Zoho Calendar's Resource Booking feature (`calendar.zoho.com/api/v1/branches`+`/resources`, office meeting rooms/equipment organized under a Branch → Building → Floor hierarchy). An empty `list_branches` result is normal -- most personal/small accounts never set this up. Each resource has an `email` -- invite it to a calendar event to book it (booking itself is a write operation, not yet built).
- `search_contacts` (matches name, email, *and* phone number server-side; searches both the Personal and Organization contact pools and merges the results, returning `{"contacts": [...], "has_more": bool}` -- `has_more` is Zoho's own signal, not inferred from a suspiciously-round result count; excludes Archived/Inactive contacts by default -- pass `status="archived"` or `status="inactive"` to search those folders instead, via `filter_type`, an undocumented Zoho param found by inspecting the real Contacts web client's network requests and confirmed against a real archived contact), `get_contact` (requires both `contact_id` and `scope`, since the same id can mean an unrelated record in the other scope -- confirmed live: fetching an org contact's id through the personal endpoint returns a 200 with a different, partial record rather than a 404), `count_contacts` (returns `{"personal": {"contacts": int, "archived": int, "inactive": int}, "organization": {...same shape...}, "total": int}` -- archived/inactive are surfaced as their own properties rather than hidden, though `total` only sums the active `contacts` count from each scope; a dedicated count lookup, no pagination/summing needed) -- Zoho Contacts, a separate Zoho product (`contacts.zoho.com`) from Mail/Calendar, sharing the same OAuth token. Each contact includes a `scope` field plus phones, notes, nickname, and birthday when set.

## Setup

1. Register a Server-based Application in the [Zoho API Console](https://accounts.zoho.com/developerconsole) with scopes `ZohoMail.messages.READ`, `ZohoMail.accounts.READ`, `ZohoMail.folders.READ`, `ZohoCalendar.event.READ`, `ZohoCalendar.calendar.READ`, `zohocontacts.contactapi.READ`, `ZohoMail.tasks.READ`, `ZohoMail.notes.READ`, `ZohoMail.links.READ`, `ZohoCalendar.resources.READ`, and `ZohoCalendar.branches.READ` (`.accounts.READ` and `.folders.READ` are also used at runtime, for `search_emails`'s timezone lookup and Sent/Drafts/Templates filtering respectively, not just at setup; `.calendar.READ` is only needed once, to look up the calendar UID in step 4), redirect URI `http://localhost:8765/callback` (change the port via `ZOHO_OAUTH_CALLBACK_PORT` if 8765 is taken).
2. Copy `.env.example` to `.env` and fill in `ZOHO_CLIENT_ID`, `ZOHO_CLIENT_SECRET`.
3. `uv run zoho-mcp-setup` -- opens your browser to approve access, stores a refresh token in the OS credential store (Windows Credential Manager via `keyring`), and prints your `ZOHO_ACCOUNT_ID`/`ZOHO_CALENDAR_UID` values (looked up automatically from your default mail account and calendar). Add both to `.env`.
4. `uv run zoho-mcp` to start the server over stdio.

Optional: set `ZOHO_STRIP_INVISIBLE_CHARS=true` in `.env` to have `get_email` strip invisible Unicode padding characters some marketing emails use to pad preview text. Off by default; never strips zero-width joiner/non-joiner, which carry real meaning in emoji sequences and some scripts.

## Development

```
uv sync
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

CI (`.github/workflows/ci.yml`) runs all of the above on every push/PR. A separate `build-validation.yml` workflow runs on `main`/tags to confirm the package builds and its entry-point modules import cleanly -- it's a release-readiness gate only, not a real deployment; there's no hosting target yet.

See [CLAUDE.md](CLAUDE.md) for the project's development rules (TDD, layered architecture, error handling, documentation).
