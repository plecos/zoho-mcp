# zoho-mcp

A vendor-agnostic MCP server exposing Zoho Mail, Zoho Calendar, and Zoho Contacts as tools to any MCP-compatible LLM client. Phase 1: Python/FastMCP, stdio transport, single-user, read-only tools.

## Development method: TDD

Every unit of behavior gets a failing test before its implementation. Build bottom-up: pure normalization functions first, then the Zoho HTTP client, then auth/token-refresh, then the MCP tool wrappers, then server wiring. Don't write implementation code with no failing test driving it.

## Architecture: separation of concerns

Nothing monolithic — each module has exactly one job:

- `zoho/client.py` — HTTP calls to Zoho Mail/Calendar/Tasks APIs and raw-JSON-to-normalized-shape conversion. No MCP/tool concepts here. Tasks (`mail.zoho.com/api/tasks`) lives here rather than its own module, unlike Contacts: it shares Mail's base domain and OAuth scope family (`ZohoMail.tasks.READ`, alongside `ZohoMail.messages.READ` etc.) rather than being a genuinely separate product/scope family.
- `zoho/contacts_client.py` — HTTP calls to the Zoho Contacts API and its normalization. Kept separate from `zoho/client.py` even though the pattern is identical: Zoho Contacts is a genuinely distinct product (own base URL `contacts.zoho.com`, own OAuth scope family `zohocontacts.contactapi.*`), not just another Mail/Calendar endpoint. The two share `zoho_authenticated_get` and `ZohoAPIError` from `zoho/client.py` rather than each rolling their own. A single account has two separate contact pools -- Personal (`self`) and Organization (`org`) -- each with its own Archived/Inactive folders; `search_contacts`/`count_contacts` query both and merge/sum. `contact_id` is **not** globally unique across the two pools (see below), so every normalized contact carries a `scope` field and `get_contact` requires it as an explicit argument rather than guessing. `search_contacts` excludes Archived/Inactive contacts by default and takes an optional `status` argument to search those folders instead, via `filter_type` -- an undocumented param not in Zoho's own published parameter list, found by inspecting the real Contacts web client's network traffic (see below).
- `zoho/auth.py` — OAuth flow and token refresh/storage. No knowledge of Mail/Calendar/Contacts payloads.
- `tools/mail.py`, `tools/calendar.py`, `tools/tasks.py`, `tools/contacts.py` — thin MCP tool wrappers that call into the relevant client and shape output for the LLM. No HTTP calls, no token logic — inject the client rather than constructing it inline.
- `server.py` — FastMCP app instantiation and tool registration only. No business logic.

If a file starts accumulating more than one of these responsibilities, split it before adding more to it. If a function is doing "fetch + parse + format + handle errors," break it apart.

## No duplicated logic

Response normalization (e.g. epoch-string dates → ISO 8601, HTML email bodies → plain text) lives in exactly one place in `zoho/client.py` and every tool calls through it. If you notice the same parsing/formatting/validation logic written more than once, extract it — don't copy-paste a variant.

## Error handling

- Never let a raw Zoho HTTP error or stack trace reach the LLM as a tool result. Catch expected failure modes (auth expired, rate-limited, not found, invalid range) and return a clear, structured error message the LLM can act on or relay to the user.
- Distinguish recoverable from unrecoverable: a token refresh should retry/re-auth transparently where possible; a malformed request should fail fast with a specific message, not a generic "something went wrong."
- Don't swallow exceptions silently — either handle them meaningfully or let them propagate with context (e.g. `raise ZohoAPIError(...) from e`).
- Validate constraints from the real API contract explicitly (e.g. the Calendar `range` param's 31-day cap) rather than letting Zoho's rejection be the first place the limit surfaces.

## Test thoroughness

Happy-path tests alone are not done. For every unit of behavior, also test:

- **Boundary conditions**: equal/inverted ranges (e.g. `end == start`, `end < start`), min/max values (e.g. Zoho's `limit` 1-200), zero/negative/huge numbers.
- **Malformed upstream data**: Zoho is a third-party API we don't control — assume it can omit a field, return the wrong type, or send an unparseable value (a non-numeric date string, a timestamp in the wrong format). Every `normalize_*` function needs a test that feeds it broken data and asserts a clean `ZohoAPIError`, not a raw `KeyError`/`ValueError` leaking out.
- **Network/transport failures**: connection errors, non-JSON responses, unexpected status codes — not just the one clean "API returned an error field" case.
- **Missing-but-optional data**: empty result sets, absent keys that should default sensibly (e.g. `data`/`events` missing entirely, not just empty).

This was found the hard way: the first pass of `list_events` validated the 31-day max but not `end <= start`, so an inverted range would have silently reached Zoho instead of failing fast — no test exercised it because every test used a valid `end > start`. Don't let that be the pattern: when writing a test for the happy path, immediately ask "what's the adjacent bad input?" and write that test too, in the same red-green cycle, not as a follow-up.

An even sharper example: `normalize_event`'s original fixture assumed top-level `start`/`end` fields with a `Z` suffix. Every real event returned by the live API instead nests them under `dateandtime.start`/`dateandtime.end`, uses a numeric UTC offset (`-0700`) instead of `Z`, and all-day events use a bare `yyyyMMdd` date with no time component at all. Every real event failed to normalize until this was caught by actually running the code against a live account.

Root-cause note, because it wasn't purely "Zoho's docs are wrong": re-checking Zoho's live docs page afterward showed it *does* document the nested `dateandtime` object (alongside an ambiguous, timezone-less top-level field) -- the first research pass missed it because it asked a fetch tool that summarizes pages through a smaller model for "the sample response" in a casual, paraphrase-inviting way, and the paraphrase dropped the nested object and fabricated a `Z` suffix that wasn't actually in the source. Two controllable takeaways, not just one:

1. **When researching an exact wire format through any summarizing tool, demand a verbatim quote, not a paraphrase.** A casual prompt gets a casual (and sometimes wrong) answer.
2. **Whether or not a vendor's docs are accurate or current is not something we control -- so don't depend on it.** Verify fixtures against at least one real response once live access exists, and lean on the error-handling/normalization layer to tolerate whatever variance shows up rather than assuming any single documented shape is the only real one. Controlling what we can control means: get the most accurate info we're able to get, and build the code to handle the weird stuff gracefully regardless.

A third example, and the sharpest one: `search_emails`'s `date` field used Zoho's `sentDateInGMT` -- which, despite its name, was not reliably GMT. Checked against `receivedTime` (Zoho's own server-side receipt timestamp) across five unrelated senders (a marketing platform, a utility company, a personal calendar invite, a bank, a courier notification), `sentDateInGMT` was consistently off by ~7.0 hours -- exactly the account's own UTC offset, on every sender, which rules out "that sender's mail server has a clock bug." This was only found because a user noticed a display timestamp that implied a future-dated email and pushed to check it against the real current time, rather than accepting "the arithmetic is internally consistent" as proof of correctness. **A field's name is not its contract.** Don't trust that a vendor field means what it's called; when a timestamp looks even slightly off, cross-check it against an independent field or the real wall clock before concluding the bug is in your conversion logic.

A fourth example: Zoho Contacts' `contact_id` looked like a stable, globally unique key -- until a live test fetched an Organization-scope contact's id through the Personal-scope single-contact endpoint and got back **HTTP 200**, not a 404, with a different, partial record for that same id (missing `emails` entirely, versus 7 emails in the real Organization record). A "try scope A, fall back to scope B on 404" design would have silently returned the wrong contact instead of failing loudly. The fix was to never let scope be inferred or guessed: every normalized contact carries the `scope` it actually came from, and `get_contact` requires it as an argument. General lesson: don't assume an id is unique just because a single endpoint treats it as a valid lookup key -- a "success" response from the wrong resource pool is a worse failure mode than a clean error, because nothing signals that anything went wrong.

A fifth example, and a genuinely new technique rather than a variation on the others: Zoho Contacts' own published parameter list (`https://www.zoho.com/contacts/api/parameters.html`) documents `include`, `page`, `per_page`, `sort`, `filter_categories`, `filter_updated_time`, `fields`, and `q` -- nothing for filtering by archived/inactive status, and the `/categories` endpoint only ever listed a "General" category. Every plausible guess (`status=archived`, `filter_status=archived`, `include=show_deleted`, `category=archived`) either 404'd or silently returned zero results. The real mechanism, `filter_type=archived`/`filter_type=inactive`, was only found by opening the actual Zoho Contacts web app in a real, logged-in browser session, clicking its own "Archived" folder, and reading the network request the page itself made. Confirmed against a contact deliberately archived for this purpose. **When a vendor's public docs don't cover a feature its own first-party web client clearly supports, that client's network traffic is the ground truth, not another round of guessing at plausible parameter names.**

A sixth example, and a genuine vendor bug rather than a docs/reality mismatch: Zoho Calendar's single-event endpoint (`GET .../events/{uid}`) accepts a `recurrenceid` query param to fetch a specific occurrence of a recurring event instead of the master record. For a timed weekly event this worked correctly. For an all-day *yearly* event, passing the correct `recurrenceid` returned the right start date but a wrong end date -- one day later than reality (`duration` came back as 2 days for a genuinely 1-day event, and `multiday` flipped to `true`), while that same occurrence's dates in Events List (`list_events`) were correct. Rather than work around an unreliable vendor field, `get_event`/`normalize_event_detail` sidesteps it entirely: it never requests or returns `start`/`end` at all. Callers get timing from `list_events` (verified correct) and use `get_event` only for what `list_events` doesn't provide -- full attendee list (`list_events` can report only the caller's own attendee entry for an occurrence, not every invitee), organizer, location, description, and the recurrence rule. **When a vendor field is measurably wrong in even one verified case, the safest fix is often to stop depending on that field at all, not to add compensating logic for the specific way it's wrong.**

One thing this does *not* mean: don't duplicate primitive type-checking that FastMCP's JSON-schema validation already does at the tool-call boundary (e.g. rejecting a non-string `query`). Focus thoroughness on business-rule constraints (ranges, ordering, bounds) and defensive handling of data from systems outside our control (Zoho's API) — not on re-validating what the MCP protocol layer already guarantees.

## Config vs. live state

Before writing a looked-up value to `.env`/config, ask: is this a **stable identifier** (account ID, calendar UID -- essentially permanent) or a **mutable setting** (timezone, a preference, anything a person can change in their account)? Only the former belongs in static config.

This was caught before shipping: the initial `days_back` implementation stored the mailbox's timezone in `ZOHO_MAILBOX_TIMEZONE`, looked up once during setup. That goes stale the moment the user changes their Zoho timezone (e.g. after moving), and nothing would signal the drift -- it would just silently misresolve "today" again, the exact bug this feature exists to fix. The fix: `ZohoClient` fetches the timezone live and caches it in memory for the life of the client instance, never persisting it to disk. Staleness is bounded to "since this process last started," not "since setup was last run," at the cost of one extra API call per client instance rather than per call.

## Don't delegate correctness to the caller

`search_emails`/`list_events` originally returned all dates/times in UTC, on the reasoning that UTC is unambiguous and the calling LLM could convert for display. In practice this was unreliable: one chat session correctly converted a UTC timestamp to Pacific time for display; a later session, with the same server and the same data, displayed the raw UTC digits mislabeled as local time with no conversion at all. Same tool, same correct data, inconsistent client behavior -- because the conversion was never actually verified by anyone, including the assistant reporting the result, until the user checked it against the real clock.

If a piece of math (timezone conversion, date-boundary resolution, unit conversion) can be done once, correctly, in tested server code, do it there -- don't return an ambiguous-to-convert value and hope every calling LLM converts it the same correct way every time. `ZohoClient` now returns every date/time already expressed in the mailbox's own local offset (fetched live, per the section above), so there is no conversion left for any caller to get right or wrong.

## Git workflow

Never commit directly to `main`. Always work on a feature branch and commit there, even for "just scaffolding" changes. `main` will eventually be a protected branch; working this way from the start means there's no habit to break later. Before any commit, scan the actual file list being staged (`git status`, `git add -A -n`) for anything that shouldn't ship: real credentials, personal/business email addresses or other identifying data in what's meant to be synthetic test fixture data, and `.gitignore` gaps (e.g. a broad pattern like `.env.*` accidentally catching `.env.example`, which should be tracked).

## Documentation

- Every public module, class, and function gets a concise docstring: what it does, params, return shape, and any exceptions it raises. Google-style docstrings.
- Don't restate what the code already makes obvious — a docstring earns its place by explaining a non-obvious contract (e.g. "raises `ZohoAuthError` if the refresh token has been revoked"), not by narrating line-by-line logic.
- No inline comments explaining *what* a line does; only comment a genuinely non-obvious *why* (a workaround, an undocumented Zoho quirk, a subtle invariant).
