"""The self-contained OAuth 2.1 authorization server (OAuth #2).

Authenticates MCP *clients* (Claude) to this server, so a hosted instance on a
public URL is not open to anyone who finds it. Entirely separate from
``zoho/auth.py``, which authenticates this server to Zoho (OAuth #1): different
tokens, different counterparties, different lifetimes.
"""
