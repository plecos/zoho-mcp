# zoho-mcp

A vendor-agnostic MCP (Model Context Protocol) server that exposes Zoho Mail and Zoho Calendar as tools to any MCP-compatible LLM client (Claude, ChatGPT, Gemini, etc.).

## Status

Phase 1: personal prototype (local stdio transport, single-user Zoho OAuth, read-only tools).

Tools implemented: `search_emails`, `get_email`, `list_events`.

## Setup

1. Register a Server-based Application in the [Zoho API Console](https://accounts.zoho.com/developerconsole) with scopes `ZohoMail.messages.READ`, `ZohoMail.accounts.READ`, `ZohoCalendar.event.READ`, and `ZohoCalendar.calendar.READ` (the `.accounts.READ` and `.calendar.READ` scopes are only needed once, to look up the IDs in step 4), redirect URI `http://localhost:8765/callback` (change the port via `ZOHO_OAUTH_CALLBACK_PORT` if 8765 is taken).
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
