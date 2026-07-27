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
- `releases.py` — which version is installed, and which one GitHub has published. Deliberately *not* under `zoho/`: it shares no base URL, no auth header and no error type with the Zoho clients, because GitHub is a different vendor. Reporting only; it never downloads or installs.
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

`send_email` delivers only if the operator set `ZOHO_ALLOW_AUTO_SEND=true`; otherwise it saves the message to Drafts and returns `"sent": False`. **The check lives in `ZohoClient`**, the layer that issues the request — not in the tool wrapper. That placement is the point: a gate in a wrapper only protects callers who go through that wrapper.

The gated path used to raise, so the safety test could assert "issued no request at all". Falling back to a draft is friendlier — the user gets the composed mail to review in a client that shows real recipients and rendering — but it means the guarantee is now carried by a *field* rather than by the absence of a call: gated calls go to the same compose endpoint with `mode: "draft"`. When you soften a gate from "refuses" to "does something safer", identify the new invariant and re-point the test at it; a test still asserting the old one passes while guarding nothing.

Two things that must stay true of the flag: it is read once at startup, and **nothing inside a conversation can change it**. Exposing it as an MCPB checkbox is fine — a human is at the settings pane. A tool that edited server config, or any `os.environ[...] = ...` in `src/`, would hand the gate to the thing the gate exists to stop. Pinned by `test_no_tool_can_turn_sending_on`.

There is deliberately no send-a-reply tool in any configuration. A reply quotes an incoming email, and incoming email is untrusted input that can contain text trying to talk an assistant into sending something. Replies always stop at Drafts.

Zoho makes sending the *default* and drafting the opt-in flag (see the notes), so this codebase inverts that and pins the inversion with tests. When a vendor makes the destructive behavior the default, invert it in your own layer and test the inversion — don't just remember to pass the flag.

### The update check is gated for a different reason

`check_for_updates` is the only thing here that contacts a host other than Zoho, so `ZOHO_CHECK_FOR_UPDATES` gates it, off by default, checked inside `ReleaseChecker` rather than in the tool wrapper — same placement argument as `send_email`. The invariant differs though: the disabled path issues **no request at all**, and that's what the test asserts. Don't copy the send gate's "assert on a field" shape here; each gate's test has to name the guarantee that gate actually makes.

**A documentation claim is an invariant.** `manifest.json` and `SECURITY.md` both said "the only outbound calls are to Zoho's own REST APIs" — the sentence someone installs this on the strength of. Adding one GitHub request made it false, so both had to be amended in the same change, and a manifest test now ties the claim to the setting: remove the exception from the prose and the test demands you remove the setting too. When a change falsifies a promise in the docs, the promise is part of the diff.

And **not everything worth detecting is worth automating.** An installed bundle *could* be made to overwrite its own directory; the reason not to isn't that it's hard, it's that a tool which fetches code from the network into the directory the server runs from is reachable from a conversation whose input includes untrusted email. That's the same hole the send gate closes. The technical obstacles are real but secondary — decide on the threat model first, or you'll spend the effort working around them.

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

Installing the MCPB bundle in a real Claude Desktop added three more, all invisible to any test:

- **The bundle installs disabled** when it has required config, and filling the config in doesn't enable it. The symptom isn't an error — the server never starts, so its tools simply aren't there and mail questions get answered by some other connector.
- **Two clients meant two server processes.** `authenticate` in one wrote the token to the credential store and updated *its own* in-memory manager; the sibling, started unauthenticated seconds earlier, refused every call until restarted. `get_access_token` now re-reads the store before giving up. Anything cached in a process is per-process — when a value can be changed by something outside that process, decide what re-reads it and when.
- **A locally installed bundle is on no update channel.** The host's `extensions-installations.json` records `"source": "local"` and `"signatureInfo": {"status": "unsigned"}` against the signed `ant.dir.*` entries that update themselves, and the manifest spec has no update field at all. It also records a content `hash` and a cached copy of the manifest, so a server that rewrote its own directory would leave the host describing a bundle that no longer exists. Detection is buildable; installation isn't — and wouldn't be wanted anyway (below).

**Watch out for a default that fills in a plausible wrong value.** `FastMCP(name)` takes no version, and the `Server` it wraps then reports the *MCP SDK's* version as the server's own. So `serverInfo.version` — the protocol's one place to state this — said `1.28.x` for the whole of 0.1.0, printed on every CI run by `scripts/smoke_bundle.py`, and read as correct because it was a plausible version number in the right shape. A field that's absent gets noticed; a field that's confidently wrong doesn't. `create_server` now sets `mcp._mcp_server.version` explicitly and a test asserts it differs from the SDK's.

## Config vs. live state

Before writing a looked-up value to `.env`, ask whether it's a **stable identifier** (account id, calendar uid — essentially permanent) or a **mutable setting** (timezone, primary address, any preference a person can change). Only the former belongs in static config.

"Belongs in static config" isn't the same as "required there". `ZOHO_ACCOUNT_ID` and `ZOHO_CALENDAR_UID` are both stable identifiers, so caching them in `.env` is safe — but requiring them made a fresh install fail with a `KeyError` until the user hand-copied two values out of setup's output. `ZohoClient` now discovers each one on first use when it's absent, so config is an optimization rather than a prerequisite. Reach for that shape whenever a required setting is something the code could just as well look up.

Caught before shipping: `days_back` originally stored the mailbox timezone in `ZOHO_MAILBOX_TIMEZONE` at setup time. That goes stale the moment the user changes their Zoho timezone, and nothing signals the drift — it would silently misresolve "today" again, the exact bug the feature existed to fix. `ZohoClient` now fetches the timezone (and the outgoing address) live, cached in memory for the life of the instance. Staleness is bounded to "since this process started" rather than "since setup was last run", at the cost of one API call per client instance rather than per call.

## Don't delegate correctness to the caller

`search_emails`/`list_events` originally returned UTC, reasoning that UTC is unambiguous and the calling LLM could convert. In practice one session converted correctly and a later session, same server and same data, displayed the raw UTC digits mislabeled as local time. Same tool, same correct data, inconsistent client behavior — because nobody verified the conversion, including the assistant reporting it.

If a piece of math (timezone conversion, date-boundary resolution, unit conversion) can be done once, correctly, in tested server code, do it there. `ZohoClient` returns every date/time already in the mailbox's own local offset, so there's no conversion left for any caller to get wrong.

## Git workflow

Never commit directly to `main`; work on a feature branch and open a PR, even for "just scaffolding" changes. Before any commit, scan the actual staged file list (`git status`, `git add -A -n`) for anything that shouldn't ship: real credentials, and real account identifiers or personal data sitting in what's meant to be synthetic fixture data. Test fixtures use obviously-fake values (`555…` ids, invented names) on purpose — keep it that way when adding fixtures.

## Releasing

The version is inert until a tag exists. `release.yml` fires only on `push:
tags: ["v*"]`, so bumping the number publishes nothing by itself — which is why
the bump belongs in **its own PR, after the features it covers have landed**,
never inside a feature PR. Two feature branches that each bump the version
collide in both files over a line that carries no functional meaning, and until
the batch is done you don't know whether it's a minor or a patch. Pre-1.0, a new
tool is additive: minor slot.

`0.2.0` lives in four places, and only two of them can fail loudly:

- [ ] `pyproject.toml` — pinned to the manifest by
      `test_the_manifest_version_matches_the_package_version`. This is also
      the one `importlib.metadata` reads, so it's what `check_for_updates`
      reports and what `serverInfo.version` carries.
- [ ] `manifest.json` — same test, **plus** release.yml's tag check
- [ ] `uv.lock` — records the project's own version, and `uv sync` rewrites it
      as soon as `pyproject.toml` changes. Nothing asks you to do it, so run
      `uv sync` after the bump rather than discovering the lockfile
      disagreeing with the package it locks.
- [ ] `README.md` — the `dist/zoho-mcp-<version>.mcpb` smoke-test example.
      **Nothing verifies this one**; it's a filename in prose, so it goes stale
      silently. It's the one to check by hand.
- [ ] `manifest.json`'s `long_description` and README's tool count ("42 tools")
      when the release adds or removes a tool. Now pinned by
      `test_the_advertised_tool_count_matches_the_tool_list`, which counts
      `manifest["tools"]` and greps both files for it — the checklist item that
      earned a test by going stale.

Then: merge the bump PR, and tag **the merge commit on `main`**, not the branch
tip — with a merge commit those are different, and tagging the tip points a
release at something that isn't in `main`'s history.

```
git checkout main && git pull --ff-only origin main
git tag v0.2.0 && git push origin v0.2.0
```

`workflow_dispatch` builds and verifies without publishing, so the pipeline can
be rehearsed without minting a release. And if a tag and the manifest ever
disagree, the build fails before publishing rather than shipping a bundle whose
filename misstates its contents.

## Documentation

- Every public module, class, and function gets a concise Google-style docstring: what it does, params, return shape, exceptions raised.
- Don't restate what the code already makes obvious. A docstring earns its place by explaining a non-obvious contract.
- No inline comments explaining *what* a line does; comment only a genuinely non-obvious *why* — a workaround, an undocumented Zoho quirk, a subtle invariant. Where a quirk has a full write-up, reference [docs/zoho-api-notes.md](docs/zoho-api-notes.md) rather than restating it.
