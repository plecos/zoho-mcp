# zoho-mcp

An MCP (Model Context Protocol) server that exposes Zoho Mail, Calendar, Contacts, Tasks, Notes, Bookmarks, Groups, and Resource Booking as tools to any MCP-compatible LLM client — Claude, ChatGPT, Gemini, or anything else that speaks MCP.

38 tools covering both reading and writing. Every one has been verified against a live Zoho account rather than built from the documentation alone; where Zoho's API behaves differently than its docs claim, [docs/zoho-api-notes.md](docs/zoho-api-notes.md) records what it actually does.

**Sending email is disabled by default.** The server saves drafts instead, and only sends if you explicitly opt in. See [Composing email](#composing-email).

## Status

Single-user, local stdio transport, personal-scale. It works and is in daily use, but there's no hosting, no multi-tenancy, and no auth beyond one account's OAuth token.

## Why not Zoho's own MCP?

Zoho ships a [first-party MCP offering](https://www.zoho.com/mcp/) covering Mail, Calendar, and ~45 other products. It's hosted, org-oriented, and broad. If you need CRM or Desk, or need several people on one shared server, use theirs.

This one is narrow on purpose: local stdio with no third-party relay, draft-first mail composition with sending off by default and no send-a-reply tool at any setting, and every date already converted to the mailbox's own local offset so no caller has to get it right. It also covers ground their pre-configured servers don't — Zoho Contacts, and Mail's Tasks, Notes, and Bookmarks.

[docs/vs-zoho-mcp.md](docs/vs-zoho-mcp.md) has the full comparison, including where theirs is the better choice and what couldn't be verified about it.

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- A Zoho account, plus a registered application in the [Zoho API Console](https://accounts.zoho.com/developerconsole)

## Setup

1. **Register a Server-based Application** in the [Zoho API Console](https://accounts.zoho.com/developerconsole) with redirect URI `http://localhost:8765/callback` (change the port with `ZOHO_OAUTH_CALLBACK_PORT` if 8765 is taken). The redirect URI is the only thing you configure there — scopes are not a console setting.

   Scopes are sent in the authorization request instead, from the `SCOPES` list in [src/zoho_mcp/setup_auth.py](src/zoho_mcp/setup_auth.py). These are what step 3 will ask you to consent to:

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

   Several are needed at runtime, not just during setup: `accounts.READ` for the live timezone and outgoing-address lookups, `folders.READ` for filtering Sent/Drafts/Templates out of search results, and `calendar.READ` for `list_calendars` (it's also what finds your calendar's UID during setup).

   If you only want read-only access, delete the `.ALL` and `.CREATE` entries from that `SCOPES` list before running setup — the read tools work fine without them, and the write tools will fail with a clear scope error.

2. **Copy `.env.example` to `.env`** and fill in `ZOHO_CLIENT_ID` and `ZOHO_CLIENT_SECRET`. Nothing else in that file is required.

3. **Run the one-time auth flow:**

   ```bash
   uv run zoho-mcp-setup
   ```

   This opens your browser to approve access, stores the refresh token in your OS credential store (via `keyring`), and prints a ready-to-paste MCP client config block with the absolute path to this install's `zoho-mcp` executable.

4. **Paste that block into your MCP client's config** and restart it. The server speaks MCP over stdio; the client launches it.

   To run it yourself instead — to check setup worked, or for a client that wants a command rather than a config file:

   ```bash
   uv run zoho-mcp
   ```

If you later add scopes, re-run `zoho-mcp-setup` — the stored token carries whatever scopes it was granted, so new ones need fresh consent.

## Tools

### Mail

| Tool | |
| --- | --- |
| `search_emails` | Keyword/sender/label search using Zoho's search syntax, optionally limited to the last N days |
| `list_emails` | Enumerate mail by read/unread status, with real pagination |
| `get_email` | Full plain-text body of one message |
| `list_attachments` | Attachment metadata (name, size) for one message |
| `get_attachment` | One attachment's content, as text when it is text |
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

`get_attachment` returns a JSON record, never a file or a blob — a tool result goes into a context window, so the bytes stop at the server. Content comes back as `text` only when it actually decodes as UTF-8; for a PDF, image, or archive you get `is_text: false` and a `note` saying what it is. Nothing here decodes or extracts text from binary formats. Zoho gives no media type on either attachment endpoint (see [the notes](docs/zoho-api-notes.md#nothing-in-the-attachment-apis-tells-you-an-attachments-type)), so `media_type` is inferred from the filename and is a hint; the decode is the fact. Text over 100,000 characters is truncated with `truncated: true`, and anything over 5 MB isn't downloaded at all.

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

All three list tools take an optional `group_id` to read a shared group's items instead of your own. `list_notes` and `list_bookmarks` additionally take `oldest_first` (and paginate with `after`); `list_tasks` does not, and paginates with `offset`. `list_tasks` takes `view="assigned_to_me"` or `"created_by_me"` for Zoho's cross-group views.

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

`ZOHO_CLIENT_ID` and `ZOHO_CLIENT_SECRET` are required. Everything else in `.env` is optional:

| Variable | Default | |
| --- | --- | --- |
| `ZOHO_ACCOUNT_ID` | *discovered* | Your Zoho Mail account id. Left unset, the server looks it up on first use and caches it for the life of the process. Setting it saves one API call per start. |
| `ZOHO_CALENDAR_UID` | *discovered* | Your default calendar's uid, same story — looked up only when a calendar tool is called without an explicit `calendar_id`. |
| `ZOHO_ALLOW_AUTO_SEND` | `false` | Allow `send_email` to actually send. See above. |
| `ZOHO_STRIP_INVISIBLE_CHARS` | `false` | Have `get_email` strip invisible Unicode padding some marketing mail uses to inflate preview text. Never touches zero-width joiner/non-joiner, which carry real meaning in emoji and several scripts. |
| `ZOHO_OAUTH_CALLBACK_PORT` | `8765` | Local port for the one-time OAuth redirect. |

Both booleans are matched case-insensitively with surrounding whitespace ignored, so `true`, `True`, and `TRUE` all enable them. Any other value leaves them off.

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

## License

[Apache License 2.0](LICENSE). Use it, modify it, ship it, sell it — commercially or otherwise.

If you redistribute it, in source or binary form, you need to: include a copy of the license, keep the existing copyright and attribution notices, mark any files you changed as modified, and carry forward the contents of [NOTICE](NOTICE). The license also grants you a patent license from the contributors, and doesn't grant any right to use the project's name or the authors' trademarks.

## Security

This server holds an OAuth token that can read and write a real mailbox, so please report vulnerabilities privately rather than in a public issue — see [SECURITY.md](SECURITY.md).

## Contributing

Contributions are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md). For anything beyond a typo, please open an issue first; this project has firm opinions about how it's built, and it's easier to sort out a mismatch in an issue than in review.
