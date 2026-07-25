# zoho-mcp

An MCP (Model Context Protocol) server that exposes Zoho Mail, Calendar, Contacts, Tasks, Notes, and Bookmarks as tools to any MCP-compatible LLM client — Claude, ChatGPT, Gemini, or anything else that speaks MCP.

37 tools covering both reading and writing. Every one has been verified against a live Zoho account rather than built from the documentation alone; where Zoho's API behaves differently than its docs claim, [docs/zoho-api-notes.md](docs/zoho-api-notes.md) records what it actually does.

**Sending email is disabled by default.** The server saves drafts instead, and only sends if you explicitly opt in. See [Composing email](#composing-email).

## Status

Single-user, local stdio transport, personal-scale. It works and is in daily use, but there's no hosting, no multi-tenancy, and no auth beyond one account's OAuth token.

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- A Zoho account, plus a registered application in the [Zoho API Console](https://accounts.zoho.com/developerconsole)

## Setup

1. **Register a Server-based Application** in the Zoho API Console with redirect URI `http://localhost:8765/callback` (change the port with `ZOHO_OAUTH_CALLBACK_PORT` if 8765 is taken).

   Request these scopes:

   ```
   ZohoMail.messages.READ      ZohoMail.messages.ALL
   ZohoMail.accounts.READ      ZohoMail.folders.READ
   ZohoMail.tags.READ
   ZohoMail.tasks.READ         ZohoMail.tasks.CREATE
   ZohoMail.notes.READ         ZohoMail.notes.CREATE
   ZohoMail.links.READ         ZohoMail.links.CREATE
   ZohoCalendar.event.READ     ZohoCalendar.event.ALL
   ZohoCalendar.calendar.READ  ZohoCalendar.freebusy.READ
   ZohoCalendar.branches.READ  ZohoCalendar.resources.READ
   zohocontacts.contactapi.READ
   ```

   A few are needed at runtime rather than just at setup: `accounts.READ` for the live timezone and outgoing-address lookups, `folders.READ` for filtering Sent/Drafts/Templates out of search results. `calendar.READ` is used once, to find your calendar's UID.

   If you only want read-only access, omit the `.ALL` and `.CREATE` scopes — the read tools work fine without them, and the write tools will fail with a clear scope error.

2. **Copy `.env.example` to `.env`** and fill in `ZOHO_CLIENT_ID` and `ZOHO_CLIENT_SECRET`.

3. **Run the one-time auth flow:**

   ```bash
   uv run zoho-mcp-setup
   ```

   This opens your browser to approve access, stores the refresh token in your OS credential store (via `keyring`), and prints your `ZOHO_ACCOUNT_ID` and `ZOHO_CALENDAR_UID`. Add both to `.env`.

4. **Start the server:**

   ```bash
   uv run zoho-mcp
   ```

   It speaks MCP over stdio. Point your client at the `zoho-mcp` executable in `.venv/Scripts/` (Windows) or `.venv/bin/` (macOS/Linux).

If you later add scopes, re-run `zoho-mcp-setup` — the stored token carries whatever scopes it was granted, so new ones need fresh consent.

## Tools

### Mail

| Tool | |
| --- | --- |
| `search_emails` | Keyword/sender/label search using Zoho's search syntax, optionally limited to the last N days |
| `list_emails` | Enumerate mail by read/unread status, with real pagination |
| `get_email` | Full plain-text body of one message |
| `list_attachments` | Attachment metadata (name, size) for one message |
| `list_folders` | All folders, with paths |
| `list_labels` | All labels/tags |
| `list_signatures` | Configured signatures, as plain text |
| `create_draft` | Save a new email as a draft *(write)* |
| `reply_draft` | Save a reply to an existing email as a draft *(write)* |
| `send_email` | Send immediately — **disabled unless opted in** *(write)* |
| `mark_as_read` / `mark_as_unread` | Flip read status on one or many messages *(write)* |
| `move_email` | Move one or many messages to a folder *(write)* |
| `add_label` / `remove_label` | Apply or remove a label on one or many messages *(write)* |

`search_emails` and `list_emails` do different jobs and aren't interchangeable. Zoho's search API has no read/unread filter and can't page past its first batch of results by recency, so it will miss older mail. Use `list_emails` whenever you need to reliably act on *every* matching message ("mark all my unread email as read"); use `search_emails` for actual searching.

The write tools take lists, and one call handles the whole batch.

### Calendar

| Tool | |
| --- | --- |
| `list_events` | Events in a time range (Zoho caps the range at 31 days) |
| `get_event` | Organizer, full attendee list, location, description, recurrence rule |
| `list_calendars` | All accessible calendars |
| `get_freebusy` | Busy slots for a given address |
| `create_event` | *(write)* |
| `update_event` | Change only the fields you pass *(write)* |
| `delete_event` | Permanent *(write)* |

`get_event` deliberately doesn't return start/end times — Zoho's single-event endpoint returns wrong dates for some recurring occurrences, so timing comes from `list_events`. `get_freebusy` only works for calendars whose owner enabled Zoho's per-calendar "include in my Free/Busy sharing" setting; it raises a clear error otherwise rather than reporting a falsely empty schedule.

### Tasks, Notes, Bookmarks

| Tool | |
| --- | --- |
| `list_tasks` / `get_task` / `create_task` | Zoho Mail's Tasks feature |
| `list_notes` / `get_note` / `create_note` | Zoho Mail's Notes feature |
| `list_bookmarks` / `get_bookmark` / `create_bookmark` | Zoho Mail's Bookmarks feature |
| `list_groups` | Shared groups you belong to |

All three list tools take an optional `group_id` to read a shared group's items instead of your own, and an `oldest_first` flag. `list_tasks` also takes `view="assigned_to_me"` or `"created_by_me"` for Zoho's cross-group views.

A Zoho group is one entity shared across all three features, so the same `group_id` works everywhere — a group can hold notes but no tasks. An empty `list_groups` is normal; most personal accounts have none.

Zoho offers no server-side filtering for task status, priority, or due date, so filter on the returned fields.

### Contacts

| Tool | |
| --- | --- |
| `search_contacts` | Matches name, email, *and* phone number |
| `get_contact` | Requires both `contact_id` and `scope` |
| `count_contacts` | Counts per scope, including archived/inactive |

An account has two separate contact pools, Personal and Organization, and a `contact_id` is **not** unique across them — the same id resolves to a different record in the other pool and returns a success, not a 404. Every contact carries the `scope` it came from, and `get_contact` requires it rather than guessing.

`search_contacts` excludes archived and inactive contacts by default; pass `status="archived"` or `"inactive"` to search those instead.

### Resource Booking

| Tool | |
| --- | --- |
| `list_branches` | Branch → Building → Floor hierarchy |
| `list_resources` | Bookable rooms/equipment |

Each resource has an email address; invite it via `create_event`'s `attendees` to book it. An empty result is normal — most accounts never configure this.

## Composing email

Mail composition is draft-first by design, because sending is the one operation here that's irreversible and reaches another person.

`create_draft` and `reply_draft` always work and always save to Drafts. `send_email` exists but refuses unless the operator sets:

```
ZOHO_ALLOW_AUTO_SEND=true
```

When that's unset, `send_email` fails before making any network call, so nothing can leave the account.

There is intentionally **no send-a-reply tool at all**, in any configuration. A reply quotes an incoming email, and email bodies are untrusted input — a message in your mailbox can contain text trying to talk an assistant into sending something on your behalf. Replies always stop at Drafts for a human to read.

Leave `ZOHO_ALLOW_AUTO_SEND` off unless you specifically want an assistant able to email people without review.

One practical note: drafts don't show up in `search_emails` at all (a Zoho quirk, not a bug here). Find them with `list_emails(folder_id=...)` using the Drafts folder id from `list_folders`.

## Configuration

All optional, in `.env`:

| Variable | Default | |
| --- | --- | --- |
| `ZOHO_ALLOW_AUTO_SEND` | `false` | Allow `send_email` to actually send. See above. |
| `ZOHO_STRIP_INVISIBLE_CHARS` | `false` | Have `get_email` strip invisible Unicode padding some marketing mail uses to inflate preview text. Never touches zero-width joiner/non-joiner, which carry real meaning in emoji and several scripts. |
| `ZOHO_OAUTH_CALLBACK_PORT` | `8765` | Local port for the one-time OAuth redirect. |

Both booleans require the exact string `true`; anything else is off.

## Development

```bash
uv sync
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy src
```

CI runs all of the above on every push and PR. A separate `build-validation.yml` workflow runs on `main` and tags to confirm the package builds and its entry points import cleanly — a release-readiness gate, not a deployment; there's no hosting target.

Conventions, architecture, and the reasoning behind the design are in [CLAUDE.md](CLAUDE.md). Zoho's API quirks — the ones that make otherwise-odd-looking code necessary — are in [docs/zoho-api-notes.md](docs/zoho-api-notes.md). Read the latter before touching anything that talks to Zoho.
