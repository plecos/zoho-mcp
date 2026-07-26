# zoho-mcp

A vendor-agnostic MCP server exposing Zoho Mail, Calendar, Contacts, Tasks, Notes, Bookmarks, Groups, and Resource Booking as tools to any MCP-compatible LLM client. Python/FastMCP, stdio transport, single-user.

Zoho-specific API behavior lives in [docs/zoho-api-notes.md](docs/zoho-api-notes.md), not here. This file is conventions. **Read the notes before changing anything that talks to Zoho** — a good deal of that code looks wrong until you know which vendor quirk it exists to handle.

## Development method: TDD

Every unit of behavior gets a failing test before its implementation. Build bottom-up: pure normalization functions first, then the Zoho HTTP client, then auth/token-refresh, then the MCP tool wrappers, then server wiring. Don't write implementation code with no failing test driving it.

## Architecture: separation of concerns

Nothing monolithic — each module has exactly one job:

- `zoho/client.py` — HTTP calls to the Zoho Mail/Calendar/Tasks/Notes/Bookmarks/Resource-Booking APIs, plus raw-JSON-to-normalized-shape conversion. No MCP or tool concepts. Tasks, Notes, and Bookmarks live here rather than in their own modules because they're Zoho *Mail* features sharing its domain and scope family, not separate products. Resource Booking is the same story for Calendar.
- `zoho/contacts_client.py` — the Zoho Contacts API and its normalization. Separate from `client.py` despite an identical pattern, because Contacts genuinely is a distinct product (own base URL, own scope family). The two share `zoho_authenticated_get` and `ZohoAPIError` rather than each rolling their own.
- `zoho/auth.py` — OAuth flow, token refresh, and token storage. No knowledge of Mail/Calendar/Contacts payloads.
- `tools/*.py` — thin MCP tool wrappers that call into a client and shape output for the LLM. No HTTP, no token logic; the client is injected, never constructed here. `tools/groups.py` is its own module because groups span Tasks/Notes/Bookmarks and belong to none of them.
- `tools/auth.py` — the `authenticate` tool: composes `zoho/auth.py`'s consent flow so an unauthorized server can be authorized from inside a conversation. Needed because an MCPB bundle has a single entry point, which puts `zoho-mcp-setup` out of reach. Its browser round trip is injectable so everything around it is testable.
- `server.py` — FastMCP instantiation and tool registration only. No business logic.

## Put the gate where the traffic passes

Twice now the right place for a check has been the one chokepoint rather than the callers. `send_email`'s gate lives in `ZohoClient`, the layer that issues the request. The unauthenticated-server error lives in `ZohoTokenManager.get_access_token`, which all 40 tools reach Zoho through — so one message covers every tool, no tool can route around it, and adding the 41st needs no thought about it.

When a rule has to hold across many call sites, find the single line they all execute. If there isn't one, that's usually the finding.

If a file starts accumulating more than one of these responsibilities, split it before adding to it. If a function is doing "fetch + parse + format + handle errors," break it apart.

## No duplicated logic

Response normalization (epoch-string dates → ISO 8601, HTML bodies → plain text) lives in exactly one place in `zoho/client.py`, and every tool calls through it. If the same parsing/formatting/validation appears more than once, extract it — don't copy-paste a variant.

## Error handling

- Never let a raw Zoho HTTP error or stack trace reach the LLM as a tool result. Catch expected failure modes (auth expired, rate-limited, not found, invalid range) and return a clear, structured message the LLM can act on or relay.
- Distinguish recoverable from unrecoverable: token refresh should retry/re-auth transparently where possible; a malformed request should fail fast with a specific message, not a generic "something went wrong."
- Don't swallow exceptions silently — handle them meaningfully or let them propagate with context (`raise ZohoAPIError(...) from e`).
- Validate constraints from the real API contract explicitly rather than letting Zoho's rejection be the first place a limit surfaces. This matters more than it sounds: Zoho frequently *accepts* invalid input silently instead of rejecting it, so an unchecked bound becomes an invisible wrong answer rather than an error.

## Read-only vs. write tools

Read-only tools use `_READ_ONLY = ToolAnnotations(readOnlyHint=True)`. Write tools get an annotation constant matching their actual semantics — `_CREATE`, `_UPDATE`, `_DELETE`, `_MAIL_UPDATE` (reversible, so `destructiveHint=False`), `_SEND` (irreversible and `openWorldHint=True`). Never reuse `_READ_ONLY` for anything that mutates, and never assume one write annotation fits every write tool.

`zoho_authenticated_get`/`_post`/`_put`/`_delete` — thin wrappers over a shared `_zoho_authenticated_request(method, ...)` — provide identical auth-header and error-wrapping for every verb, with support for both query params and JSON bodies (Zoho needs both, depending on the product). Add a new verb wrapper here rather than open-coding `http_client.post(...)` elsewhere.

### Sending email is gated on purpose

`send_email` refuses unless the operator sets `ZOHO_ALLOW_AUTO_SEND=true`, and **the check lives in `ZohoClient`**, the layer that issues the request — not in the tool wrapper. That placement is the point: a gate in a wrapper only protects callers who go through that wrapper.

There is deliberately no send-a-reply tool in any configuration. A reply quotes an incoming email, and incoming email is untrusted input that can contain text trying to talk an assistant into sending something. Replies always stop at Drafts.

Zoho makes sending the *default* and drafting the opt-in flag (see the notes), so this codebase inverts that and pins the inversion with tests. When a vendor makes the destructive behavior the default, invert it in your own layer and test the inversion — don't just remember to pass the flag.

## Test thoroughness

Happy-path tests alone are not done. For every unit of behavior, also test:

- **Boundary conditions** — equal/inverted ranges (`end == start`, `end < start`), documented min/max values, zero/negative/huge numbers.
- **Malformed upstream data** — Zoho is a third party we don't control; assume it can omit a field, return the wrong type, or send an unparseable value. Every `normalize_*` function needs a test feeding it broken data and asserting a clean `ZohoAPIError`, not a leaked `KeyError`/`ValueError`.
- **Network/transport failures** — connection errors, non-JSON responses, unexpected status codes, not just the one clean "API returned an error field" case.
- **Missing-but-optional data** — empty result sets, absent keys that should default sensibly (`data`/`events` missing entirely, not merely empty).

This came from a real miss: the first pass of `list_events` validated the 31-day cap but not `end <= start`, because every test used a valid range. When writing the happy-path test, immediately ask "what's the adjacent bad input?" and write that too, in the same red-green cycle.

What this does *not* mean: don't duplicate primitive type-checking that FastMCP's JSON-schema validation already does at the tool-call boundary. Focus on business-rule constraints (ranges, ordering, bounds) and on defending against data from systems outside our control.

## Verify against the live API, not the docs

The single most productive rule in this project. Zoho's documentation is wrong often enough — not vague, *wrong* — that no feature is considered done until it has run against a real account. [docs/zoho-api-notes.md](docs/zoho-api-notes.md) is the accumulated evidence: required fields documented as optional, fields whose names contradict their contents, a mandatory field with a typo in its name, endpoints listed at URLs that 404.

Practices that follow from it:

- **Demand verbatim quotes when researching a wire format.** A summarizing fetch tool asked casually for "the sample response" will paraphrase, drop nested objects, and invent fields that aren't in the source. That's not hypothetical — it's how `normalize_event` shipped with a fabricated `Z` suffix and no `dateandtime` object.
- **A field's name is not its contract.** `sentDateInGMT` isn't GMT; `previousFolderId` isn't a parent; `isPrev` isn't a paging direction.
- **When a vendor error is content-free, find one that isn't and diff them.** Zoho's generic `Invalid Input` says nothing, but `EXTRA_KEY_FOUND_IN_JSON` fires only for unrecognized keys — so probing one key at a time separates "wrong name" from "right name, wrong value", turning an unbounded guess into two bounded searches.
- **"Optional" means "the doc author believed it was optional."** Treat a documented-optional field that appears on every real record as a candidate required field the moment a minimal request fails.
- **Prefer dropping a field to compensating for it.** When a vendor field is measurably wrong in even one verified case, stop depending on it rather than adding logic for the specific way it's wrong.
- **When a tool can't do something, look for a different endpoint before working around it.** Search and List are separate Zoho APIs with different capabilities, not one API with an artificial cap.
- **Two endpoints sharing a vendor, a base URL, and a field name are still two independent contracts.** Verify each one's real types; a sibling that looks identical isn't evidence.
- **When the vendor's docs don't cover something its own web client does, read that client's network traffic.** That's ground truth, and it beats another round of guessing at parameter names.
- **A negative result from a test that queried the wrong field is not evidence of absence.** When a "confirmed" finding later becomes inconvenient, that's the moment to re-derive it, not to lean on it.
- **Rule out your own test data before concluding the vendor is broken.** A single live sample confirming a hypothesis is not confirmation of the hypothesis — especially when you created that sample.
- **If behavior can't be distinguished with the data available, say so and wait.** Don't ship the likelier-looking reading of an ambiguous parameter. And when you do ship something unverified, label the *assumption the design rests on*, not just the fields being parsed — the structural assumption is the part no amount of re-reading the docs will settle.

## Watch a real client use the tools

Two design flaws in this project were invisible to a passing test suite and obvious within minutes of driving the server from an actual MCP client:

- The mail write tools each took a single `message_id`, so asked to mark ~35 emails read, the client looped 35 sequential calls. Zoho's endpoint accepted a batch all along. **A per-item tool shape is only right when the underlying API is per-item** — copying a sibling tool's shape copied the wrong precedent.
- That same session exposed Search's missing pagination, because the sweep kept missing older unread mail.

Neither was a coverage gap. Every test passed, each exercising a single id. Periodically drive the tools at realistic scale and eyeball the real output.

## Config vs. live state

Before writing a looked-up value to `.env`, ask whether it's a **stable identifier** (account id, calendar uid — essentially permanent) or a **mutable setting** (timezone, primary address, any preference a person can change). Only the former belongs in static config.

"Belongs in static config" isn't the same as "required there". `ZOHO_ACCOUNT_ID` and `ZOHO_CALENDAR_UID` are both stable identifiers, so caching them in `.env` is safe — but requiring them made a fresh install fail with a `KeyError` until the user hand-copied two values out of setup's output. `ZohoClient` now discovers each one on first use when it's absent, so config is an optimization rather than a prerequisite. Reach for that shape whenever a required setting is something the code could just as well look up.

Caught before shipping: `days_back` originally stored the mailbox timezone in `ZOHO_MAILBOX_TIMEZONE` at setup time. That goes stale the moment the user changes their Zoho timezone, and nothing signals the drift — it would silently misresolve "today" again, the exact bug the feature existed to fix. `ZohoClient` now fetches the timezone (and the outgoing address) live, cached in memory for the life of the instance. Staleness is bounded to "since this process started" rather than "since setup was last run", at the cost of one API call per client instance rather than per call.

## Don't delegate correctness to the caller

`search_emails`/`list_events` originally returned UTC, reasoning that UTC is unambiguous and the calling LLM could convert. In practice one session converted correctly and a later session, same server and same data, displayed the raw UTC digits mislabeled as local time. Same tool, same correct data, inconsistent client behavior — because nobody verified the conversion, including the assistant reporting it.

If a piece of math (timezone conversion, date-boundary resolution, unit conversion) can be done once, correctly, in tested server code, do it there. `ZohoClient` returns every date/time already in the mailbox's own local offset, so there's no conversion left for any caller to get wrong.

## Git workflow

Never commit directly to `main`; work on a feature branch and open a PR, even for "just scaffolding" changes. Before any commit, scan the actual staged file list (`git status`, `git add -A -n`) for anything that shouldn't ship: real credentials, and real account identifiers or personal data sitting in what's meant to be synthetic fixture data. Test fixtures use obviously-fake values (`555…` ids, invented names) on purpose — keep it that way when adding fixtures.

## Documentation

- Every public module, class, and function gets a concise Google-style docstring: what it does, params, return shape, exceptions raised.
- Don't restate what the code already makes obvious. A docstring earns its place by explaining a non-obvious contract.
- No inline comments explaining *what* a line does; comment only a genuinely non-obvious *why* — a workaround, an undocumented Zoho quirk, a subtle invariant. Where a quirk has a full write-up, reference [docs/zoho-api-notes.md](docs/zoho-api-notes.md) rather than restating it.
