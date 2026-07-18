# zoho-mcp

A vendor-agnostic MCP server exposing Zoho Mail and Zoho Calendar as tools to any MCP-compatible LLM client. Phase 1: Python/FastMCP, stdio transport, single-user, read-only tools.

## Development method: TDD

Every unit of behavior gets a failing test before its implementation. Build bottom-up: pure normalization functions first, then the Zoho HTTP client, then auth/token-refresh, then the MCP tool wrappers, then server wiring. Don't write implementation code with no failing test driving it.

## Architecture: separation of concerns

Nothing monolithic — each module has exactly one job:

- `zoho/client.py` — HTTP calls to Zoho Mail/Calendar APIs and raw-JSON-to-normalized-shape conversion. No MCP/tool concepts here.
- `zoho/auth.py` — OAuth flow and token refresh/storage. No knowledge of Mail/Calendar payloads.
- `tools/mail.py`, `tools/calendar.py` — thin MCP tool wrappers that call into `zoho/client.py` and shape output for the LLM. No HTTP calls, no token logic — inject the client rather than constructing it inline.
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

One thing this does *not* mean: don't duplicate primitive type-checking that FastMCP's JSON-schema validation already does at the tool-call boundary (e.g. rejecting a non-string `query`). Focus thoroughness on business-rule constraints (ranges, ordering, bounds) and defensive handling of data from systems outside our control (Zoho's API) — not on re-validating what the MCP protocol layer already guarantees.

## Git workflow

Never commit directly to `main`. Always work on a feature branch and commit there, even for "just scaffolding" changes. `main` will eventually be a protected branch; working this way from the start means there's no habit to break later. Before any commit, scan the actual file list being staged (`git status`, `git add -A -n`) for anything that shouldn't ship: real credentials, personal/business email addresses or other identifying data in what's meant to be synthetic test fixture data, and `.gitignore` gaps (e.g. a broad pattern like `.env.*` accidentally catching `.env.example`, which should be tracked).

## Documentation

- Every public module, class, and function gets a concise docstring: what it does, params, return shape, and any exceptions it raises. Google-style docstrings.
- Don't restate what the code already makes obvious — a docstring earns its place by explaining a non-obvious contract (e.g. "raises `ZohoAuthError` if the refresh token has been revoked"), not by narrating line-by-line logic.
- No inline comments explaining *what* a line does; only comment a genuinely non-obvious *why* (a workaround, an undocumented Zoho quirk, a subtle invariant).
