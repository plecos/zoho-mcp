# Contributing to zoho-mcp

Contributions are welcome. Two things are worth knowing before you spend time
on a change.

## Licensing

The project is [MIT](LICENSE) licensed, and contributions are accepted under the
same terms — by opening a pull request you agree your contribution is licensed
that way. You keep the copyright in your work; you'll be credited in the git
history like any other contributor. There's no CLA and nothing to sign.

## Open an issue before writing code

For anything beyond a typo, please discuss it first. This project has firm
opinions about how it's built, and a change that doesn't fit them is a waste of
your effort — easier to sort out in an issue than in review.

## Ground rules

Read [CLAUDE.md](CLAUDE.md) before writing anything. It documents the
architecture and conventions, and they aren't negotiable in review. The
short version:

- **Test first.** Every behavior gets a failing test before its implementation.
  A PR whose tests were written after the fact is hard to distinguish from one
  whose tests were written to match a bug.
- **Respect the layering.** HTTP and normalization in `zoho/`, LLM-facing
  shaping in `tools/`, registration only in `server.py`. No HTTP calls in a
  tool wrapper; no MCP concepts in a client.
- **Errors reach the model as `ZohoAPIError`,** never as a raw HTTP failure or
  traceback.
- **Test the unhappy paths too** — boundaries, malformed upstream data,
  transport failures, empty results. Zoho's API is a third party we don't
  control, and it is genuinely strange (see below).

### If your change touches Zoho's API, read the field notes first

[docs/zoho-api-notes.md](docs/zoho-api-notes.md) records behavior verified
against a live account, much of which contradicts Zoho's documentation:
required fields documented as optional, fields whose names contradict their
contents, endpoints listed at URLs that return 404.

A fair amount of this codebase looks wrong until you know which quirk it exists
to handle. Please don't "simplify" something into a bug — if a normalizer or a
payload looks needlessly defensive, check the notes before removing it.

The corollary: **verify new behavior against a real Zoho account, not against
the documentation.** A PR that only proves Zoho's docs were followed is not
evidence the code works, and past experience says it's roughly even odds either
way.

## Before opening a pull request

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy src
```

CI runs all four and they must pass.

Don't include real account data — email addresses, account or folder ids, or
message contents — in tests, fixtures, or commit messages. Fixtures use
obviously synthetic values (`555…` ids, invented names) on purpose.

## Reporting a security issue

Please don't open a public issue. Email `plecos@thesalters.net` with
details and we'll respond as quickly as we can.

This server holds an OAuth token with read and write access to a real mailbox
and calendar, so anything touching token storage, the auth flow, or the
send-email gate is worth reporting privately.
