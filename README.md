# zoho-mcp

A vendor-agnostic MCP (Model Context Protocol) server that exposes Zoho Mail and Zoho Calendar as tools to any MCP-compatible LLM client (Claude, ChatGPT, Gemini, etc.).

## Status

Phase 1: personal prototype (local stdio transport, single-user Zoho OAuth, read-only tools).

Tools implemented: `search_emails`, `get_email`, `list_events`.

## Setup

1. Register a Server-based Application in the [Zoho API Console](https://accounts.zoho.com/developerconsole) with scopes `ZohoMail.messages.READ` and `ZohoCalendar.event.READ`, redirect URI `http://localhost:8765/callback` (change the port via `ZOHO_OAUTH_CALLBACK_PORT` if 8765 is taken).
2. Copy `.env.example` to `.env` and fill in `ZOHO_CLIENT_ID`, `ZOHO_CLIENT_SECRET`, `ZOHO_ACCOUNT_ID`, `ZOHO_CALENDAR_UID`.
3. `uv run zoho-mcp-setup` -- opens your browser to approve access, then stores a refresh token in the OS credential store (Windows Credential Manager via `keyring`).
4. `uv run zoho-mcp` to start the server over stdio.

## Development

```
uv sync
uv run pytest
```

See [CLAUDE.md](CLAUDE.md) for the project's development rules (TDD, layered architecture, error handling, documentation).
