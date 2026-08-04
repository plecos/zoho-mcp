# zoho-mcp

An MCP (Model Context Protocol) server that exposes Zoho Mail, Calendar, Contacts, Tasks, Notes, Bookmarks, Groups, and Resource Booking as tools to any MCP-compatible LLM client — Claude, ChatGPT, Gemini, or anything else that speaks MCP.

42 tools covering both reading and writing. Every one has been verified against a live Zoho account rather than built from the documentation alone; where Zoho's API behaves differently than its docs claim, [docs/zoho-api-notes.md](docs/zoho-api-notes.md) records what it actually does.

**Sending email is disabled by default.** The server saves drafts instead, and only sends if you explicitly opt in. See [Composing email](#composing-email).

## Status

Single-user, local stdio transport, personal-scale. It works and is in daily use, but there's no hosting, no multi-tenancy, and no auth beyond one account's OAuth token.

## Why not Zoho's own MCP?

Zoho ships a [first-party MCP offering](https://www.zoho.com/mcp/) covering Mail, Calendar, and ~45 other products. It's hosted, org-oriented, and broad. If you need CRM or Desk, or need several people on one shared server, use theirs.

This one is narrow on purpose: local stdio with no third-party relay, draft-first mail composition with sending off by default and no send-a-reply tool at any setting, and every date already converted to the mailbox's own local offset so no caller has to get it right. It also covers ground their pre-configured servers don't — Zoho Contacts, and Mail's Tasks, Notes, and Bookmarks.

[docs/vs-zoho-mcp.md](docs/vs-zoho-mcp.md) has the full comparison, including where theirs is the better choice and what couldn't be verified about it.

## Requirements

- [uv](https://docs.astral.sh/uv/) — required for both install methods, including the packaged extension
- **On Linux, a Secret Service keyring backend** (gnome-keyring, KWallet, or the `keyrings.alt` package). The refresh token lives in the OS credential store, and headless servers, minimal desktops, WSL and containers often have no backend at all. Without one the server still starts and reports itself unauthenticated, but `authenticate` cannot save its result. macOS and Windows always have one.
- Python 3.12+ — only if you run from a checkout; uv provides its own interpreter for the extension
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

If you later add scopes, re-run `zoho-mcp-setup` — or just call the `authenticate` tool, which does the same thing from inside a conversation. The stored token carries whatever scopes it was granted, so new ones need fresh consent.

### Installing as a Claude Desktop extension

The repo also packages as an [MCP Bundle](https://github.com/anthropics/mcpb), which replaces steps 2–4 with a settings form. Build one with [the MCPB CLI](https://github.com/anthropics/mcpb):

```bash
npx @anthropic-ai/mcpb pack
```

Or download a prebuilt one from [Releases](https://github.com/plecos/zoho-mcp/releases).

That produces a `zoho-mcp.mcpb` you can install from Claude Desktop's Extensions pane. It declares `server.type: "uv"`, so dependencies are resolved from `pyproject.toml` at install time rather than vendored into the archive.

**You need [uv](https://docs.astral.sh/uv/) on your PATH; you do not need Python.** Claude Desktop does not ship a uv of its own — verified on a real install, where the extension's generated `pyvenv.cfg` recorded the same uv version as the one in the user's own `~/.local/bin`. uv then downloads and manages its own CPython, so no system Python is involved. The MCPB documentation's "no user Python installation required" is accurate about Python and silent about uv.

**One bundle covers Windows, macOS and Linux.** The archive holds only Python source, a manifest and a universal `uv.lock`; there is nothing compiled in it, and the host resolves dependencies for its own platform at install time. Per-platform downloads would be identical files under names implying otherwise. The release workflow proves this rather than asserting it — the same artifact is unpacked and launched on Windows, Apple Silicon macOS and Linux, and the release only publishes if all three complete an MCP handshake.

You still register an application in the Zoho API Console (step 1) and paste its client id and secret into the extension's settings, where the secret is stored in your OS credential store. Then call the **`authenticate`** tool once: it opens Zoho's own consent page in your browser, and the resulting token goes to the credential store too. Nothing is typed into the conversation.

Several lifecycle quirks of the host, all worth knowing before you install:

**You have to enable it manually.** Because the extension has required configuration, Claude Desktop installs it *disabled* and logs `has missing required configuration, not enabling automatically`. Filling the fields in afterwards does not flip the toggle — you have to switch it on yourself. Until you do, the server never launches, none of its tools appear, and requests about your mail silently go to whatever other mail connector you have enabled.

**To uninstall or replace it, disable it and restart first.** Uninstalling a running extension can fail — on Windows the live server holds files inside its own directory open (`.venv\Scripts\*.exe`, loaded extension modules), and the host does not appear to retry the delete. Disable the extension, restart Claude, then uninstall.

**Disabling stops the tools, not the process.** The restart in that sequence is doing real work, not being cautious. Turning the toggle off withdraws the tools from conversations immediately — ask for one and the client reports it gone — but the server process it already spawned keeps running until Claude Desktop is quit; measured still alive five minutes after the toggle, and in another case until the app was closed. So a disabled extension is still holding its own directory open, which is precisely what makes the uninstall fail. The symptom is misleading in the safe direction (the tools really are unreachable), but "disabled" is not "stopped".

The server itself exits promptly when the host closes its stdin, which is the only shutdown signal MCP defines — measured at 0.08 s for the whole process tree, with no orphans — so there's nothing to wait for on this side.

**Uninstalling clears your settings.** The client id, secret, port and toggle live in the host's per-extension settings file, which is deleted with the extension; reinstalling means re-entering them. The refresh token is separate — it's in your OS credential store — so it survives an uninstall and you won't need to run `authenticate` again.

**Installing a bundle whose version matches the installed one uninstalls it.** Opening a `zoho-mcp-0.1.0.mcpb` while `0.1.0` is already installed does not replace it in place: the host removes the extension directory *and* its settings file, then waits for you to confirm a fresh install. Combined with the quirk above, that means re-entering the client id and secret. Bump the version before rebuilding if you want to test an upgrade rather than perform an uninstall — observed on macOS with Claude Desktop 1.24012.9.

**Changing a setting doesn't restart the server, and neither does reinstalling.** The host substitutes `user_config` values into the environment when it *spawns* the server process, and `_build_zoho_clients_from_env` reads them once at startup. A running server therefore keeps the environment it was launched with: tick the send checkbox and the live process still has the old value — or, after a reinstall, no value at all for a variable the new manifest introduced. Quit and reopen Claude Desktop to pick up a settings change. Nothing surfaces as an error, so the symptom is a setting that appears on and behaves off.

**How the host renders each `user_config` type into the environment** — read out of a live server's own process environment rather than inferred from the spec, since the substitution happens in the host and nothing documents it:

| Declared type | Stored in settings as | Arrives in the environment as |
| --- | --- | --- |
| `boolean` | JSON `true` / `false` | `true` / `false`, lowercase |
| `number` | JSON `8765` | `8765` — no decimal point |

Both parsers here are written for that and fail closed on anything else, so a host that ever changed this would leave a toggle inert rather than silently on.

Sending email is one of those settings — a checkbox, off when you install it. It used to be withheld from the settings form on the theory that hand-editing `.env` was useful friction, but for a bundle install the file would have to live inside the installed extension directory, which every update replaces. That isn't friction, it's a feature you can't reach. What keeps mail in your account is that the box starts unticked and nothing but you can tick it — no tool changes server settings.

## Updating

**Nothing updates itself.** A bundle you installed from a file is on no update channel, so you find out about a new version by looking — either at the [releases page](https://github.com/plecos/zoho-mcp/releases), or by asking the assistant, which is what `check_for_updates` is for.

That tool is off until you tick **"Check for new versions of this extension"**. Left off it reports the version you're running and points at the releases page, without a network call. Turned on it asks GitHub's public releases API for the latest tag and compares it — one request, no account data in it, cached for an hour. It never downloads or installs anything.

To update: download the new `.mcpb`, **disable the extension and restart Claude first** (see the uninstall quirk above), then open the file. Expect to re-enter your client id and secret; your Zoho authorization survives, so `authenticate` doesn't need re-running. Then quit and reopen Claude so the new process picks the settings up.

**Why there's no tool that does it for you.** Four reasons, in ascending order of how much they matter:

- The MCPB manifest format has no update field — no update URL, nothing that tells a host where a newer bundle lives.
- Automatic updates are a channel for extensions installed from Anthropic's directory. A locally installed one records `"source": "local"` and `"signatureInfo": {"status": "unsigned"}` in the host's `extensions-installations.json`, against the signature info that directory extensions carry.
- That same file records a content `hash` of what was installed plus a cached copy of its manifest. A server that unzipped a new version over its own directory would desync both, leaving the host's UI, version and tool list describing the bundle it thinks it installed. It also couldn't restart itself — the host owns the process lifecycle — and on Windows it would be overwriting files it holds open.
- Most of all: it would be the wrong shape. A tool that fetches code from the network into the directory the server runs from is reachable from inside a conversation, and this server's input includes email, which is untrusted. That's the same hole the send gate exists to close. It stays closed.

## Tools

### Authorization

| Tool | |
| --- | --- |
| `authenticate` | Run the Zoho consent flow and store the token *(write)* |

Only needed if the server was started without a stored token — the case for a bundle install, where `zoho-mcp-setup` isn't reachable. Every other tool fails with a message naming this one until it has run. The check lives in the token manager, which every Zoho call passes through, so no tool can route around it.

### Version

| Tool | |
| --- | --- |
| `check_for_updates` | Report whether a newer version has been published |

The only tool that contacts a host other than Zoho, and the only one gated on a setting that starts off. See [Updating](#updating).

### Mail

| Tool | |
| --- | --- |
| `search_emails` | Keyword/sender/label search using Zoho's search syntax, optionally limited to the last N days |
| `list_emails` | Enumerate mail by read/unread status, with real pagination |
| `get_email` | Full plain-text body of one message |
| `get_email_source` | Parsed RFC 822 headers: sender, SPF/DKIM/DMARC, `Received` chain |
| `list_attachments` | Attachment metadata (name, size) for one message |
| `get_attachment` | One attachment's content, as text when it is text |
| `list_folders` | All folders, with paths |
| `list_labels` | All labels/tags |
| `list_signatures` | Configured signatures, as plain text |
| `create_draft` | Save a new email as a draft *(write)* |
| `reply_draft` | Save a reply to an existing email as a draft *(write)* |
| `forward_draft` | Forward an email as a draft, keeping its formatting and attachments *(write)* |
| `send_email` | Send immediately — **drafts it instead unless opted in** *(write)* |
| `mark_as_read` / `mark_as_unread` | Flip read status on one or many messages *(write)* |
| `move_email` | Move one or many messages to a folder *(write)* |
| `add_label` / `remove_label` | Apply or remove a label on one or many messages *(write)* |

`search_emails` and `list_emails` do different jobs and aren't interchangeable. Zoho's search API has no read/unread filter and can't page past its first batch of results by recency, so it will miss older mail. Use `list_emails` whenever you need to reliably act on *every* matching message ("mark all my unread email as read"); use `search_emails` for actual searching.

The write tools take lists, and one call handles the whole batch.

`snippet` always comes back with runs of whitespace collapsed to a single space. Marketing mail pads preview text heavily, and a real example measured 249 characters of which ~200 were padding — some invisible, some `U+2007` FIGURE SPACE and similar width-having variants that no list of codepoints keeps up with. Collapsing runs discards nothing (every mail client does it visually) and catches all of them. Removing the *invisible* characters does change content, so that part stays opt-in via `ZOHO_STRIP_INVISIBLE_CHARS`. Neither touches `subject`.

`search_emails` and `list_emails` return 15 fields per message — id, from, from_name, subject, date, snippet, folder_id, read, to, cc, has_attachment, size_bytes, label_ids, flag, priority.

`priority` is one of `highest`/`high`/`normal`/`low`/`lowest`, converted from Zoho's numeric field — `high`, `normal` and `low` are each [verified against the `Importance` header](docs/zoho-api-notes.md#priority-is-x-priority-confirmed-against-the-importance-header) on real messages, and those three are the range Zoho's own compose UI produces. `flag` is Zoho's flag *type* name (`important`, `followup` or `info` — Zoho's three flag types, lowercase and unseparated) and is empty when a message isn't flagged. Bear in mind that `priority` is whatever the *sender* claimed, while `flag` is the account owner's own marking. Zoho sends 21 fields; the six still omitted are omitted for [stated reasons](docs/zoho-api-notes.md#the-thread-fields-still-could-not-be-verified) — `sentDateInGMT` is wrong, the thread fields can't be read from any available data, and the rest are undocumented with no evident use.

`get_email_source` answers the questions `get_email` can't: who really sent this, did it pass SPF/DKIM/DMARC, what path did it take. Zoho returns the whole message source — 28,000 to 82,000 characters for ordinary mail — so it's parsed here into headers, an ordered `Received` chain, and the names of the headers not returned by value. RFC 2047 encoded-words are decoded. `include_raw=true` adds the source text itself, capped.

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

`create_draft`, `reply_draft` and `forward_draft` always work and always save to Drafts. `send_email` only delivers if the operator has turned sending on — the "Let the assistant send email without your review" checkbox in a bundle install, or:

```
ZOHO_ALLOW_AUTO_SEND=true
```

With sending off, `send_email` doesn't fail: it saves the message to Drafts and returns `"sent": false` with a note saying where it went. You get the composed mail to review in your own client, where you can see the real recipients and rendering, rather than losing the work to an error. Nothing can leave the account either way — the request Zoho receives carries `mode: "draft"` on every gated call.

There is intentionally **no send-a-reply or send-a-forward tool at all**, in any configuration. Both carry an incoming email, and email bodies are untrusted input — a message in your mailbox can contain text trying to talk an assistant into sending something on your behalf. Replies and forwards always stop at Drafts for a human to read.

Use `forward_draft` to forward mail rather than reading a message and recomposing it with `create_draft`. It copies the original's real HTML into the draft, so formatting survives; `get_email` returns plain text, so anything rebuilt from it arrives stripped — which is exactly the bug this tool exists to fix.

There's more machinery behind that than the name suggests. Zoho's own `action=forward` API returns a content-free 500 on every valid request, and Zoho's web client doesn't use it either — it assembles the forwarded body in the browser and posts the result. `forward_draft` does the same thing server-side, and copies the original's attachments across by downloading and re-uploading each one. Attachments too large to relay are refused with a clear error rather than quietly dropped, so a draft that comes back is a complete forward.

Inline images survive too: Zoho turns the original's image references into real MIME parts when the message is finally sent. That was confirmed by sending one and reading the delivered source, not assumed.

Leave sending off unless you specifically want an assistant able to email people without review.

One practical note: drafts don't show up in `search_emails` at all (a Zoho quirk, not a bug here). Find them with `list_emails(folder_id=...)` using the Drafts folder id from `list_folders`.

## Configuration

`ZOHO_CLIENT_ID` and `ZOHO_CLIENT_SECRET` are required (a bundle install collects them in its settings form instead of `.env`). Everything else is optional:

| Variable | Default | |
| --- | --- | --- |
| `ZOHO_ACCOUNT_ID` | *discovered* | Your Zoho Mail account id. Left unset, the server looks it up on first use and caches it for the life of the process. Setting it saves one API call per start. |
| `ZOHO_CALENDAR_UID` | *discovered* | Your default calendar's uid, same story — looked up only when a calendar tool is called without an explicit `calendar_id`. |
| `ZOHO_ALLOW_AUTO_SEND` | `false` | Allow `send_email` to actually send. Left off, it saves to Drafts instead. Exposed as a checkbox in a bundle install. See above. |
| `ZOHO_STRIP_INVISIBLE_CHARS` | `false` | Strip the invisible Unicode padding some marketing mail uses to inflate preview text — from `get_email` bodies and from the `snippet` of every `search_emails`/`list_emails` result. Subjects are left alone; senders don't pad those, since it would look broken in any mail client. Never touches zero-width joiner/non-joiner, which carry real meaning in emoji and several scripts. |
| `ZOHO_CHECK_FOR_UPDATES` | `false` | Let `check_for_updates` ask GitHub's releases API whether a newer version exists. The one setting that permits an outbound call to a host other than Zoho. Exposed as a checkbox in a bundle install. See [Updating](#updating). |
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

### Cutting a release

Bump `version` in both `pyproject.toml` and `manifest.json` (a test enforces that they match), then push a tag:

```bash
git tag v0.3.1 && git push origin v0.3.1
```

`release.yml` validates the manifest, checks the version against the tag, packs the bundle, refuses to publish one containing a `.env` or a virtualenv, smoke-tests it on all three platforms, and only then creates the GitHub release with the `.mcpb` attached. `workflow_dispatch` runs everything except the publish, so the pipeline can be exercised without minting a release.

To smoke-test a bundle yourself:

```bash
uv run python scripts/smoke_bundle.py dist/zoho-mcp-0.3.1.mcpb
```

It launches the bundle using the command its own `manifest.json` declares, so a broken `mcp_config` fails there rather than after someone installs it.

Conventions, architecture, and the reasoning behind the design are in [CLAUDE.md](CLAUDE.md). Zoho's API quirks — the ones that make otherwise-odd-looking code necessary — are in [docs/zoho-api-notes.md](docs/zoho-api-notes.md). Read the latter before touching anything that talks to Zoho.

## License

[Apache License 2.0](LICENSE). Use it, modify it, ship it, sell it — commercially or otherwise.

If you redistribute it, in source or binary form, you need to: include a copy of the license, keep the existing copyright and attribution notices, mark any files you changed as modified, and carry forward the contents of [NOTICE](NOTICE). The license also grants you a patent license from the contributors, and doesn't grant any right to use the project's name or the authors' trademarks.

## Security

This server holds an OAuth token that can read and write a real mailbox, so please report vulnerabilities privately rather than in a public issue — see [SECURITY.md](SECURITY.md).

## Contributing

Contributions are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md). For anything beyond a typo, please open an issue first; this project has firm opinions about how it's built, and it's easier to sort out a mismatch in an issue than in review.
