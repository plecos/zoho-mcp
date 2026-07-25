# Security Policy

## Reporting a vulnerability

**Please don't open a public issue.**

Report privately via [GitHub's private vulnerability reporting](https://github.com/plecos/zoho-mcp/security/advisories/new), or by email to **plecos@thesalters.net** with `zoho-mcp security` in the subject.

Helpful to include: what you found, how to reproduce it, and what an attacker could do with it. A proof of concept is welcome but not required — a clear description beats no report.

This is a personal project with a single maintainer, so there's no formal SLA. Realistically: an acknowledgement within a few days, and a fix prioritized by severity. If something is being actively exploited, say so in the subject line. If you don't hear back within a week, follow up — assume it was missed, not ignored.

You're welcome to disclose publicly once a fix is released, or after 90 days if nothing has happened. Credit is offered by default; tell me if you'd rather stay anonymous.

## Why this project deserves care

Worth stating plainly, because it shapes what counts as a vulnerability here: **this server hands an LLM read and write access to a real mailbox and calendar.**

A working install holds an OAuth token that can read every email in the account, read contacts and calendars, move and relabel messages, delete calendar events, and — if explicitly enabled — send email as the account owner. A compromise isn't "a bug in a utility"; it's access to someone's correspondence.

That token is the asset an attacker wants. Anything that exposes it, or that causes the server to act on instructions its owner never gave, is worth reporting.

## In scope

- Exposure of the refresh token, access token, or client secret — in logs, error messages, tracebacks, crash dumps, temp files, or process listings
- Flaws in the OAuth flow: the local callback listener, code exchange, or token storage
- **Bypasses of the send gate** — anything that causes `send_email` to deliver mail when `ZOHO_ALLOW_AUTO_SEND` is not set to `true`
- Paths by which untrusted content (an email body, a calendar invite, a contact's notes) could cause the server itself to take an action beyond what the tool was asked to do
- Account data leaking where it shouldn't — into error messages returned to the model, into fixtures, or into the repository
- Vulnerable dependencies with a plausible path to exploitation here
- A tool doing materially more than its description implies — e.g. a "read" tool that writes

## Out of scope

- **Vulnerabilities in Zoho's own APIs or services.** Report those to [Zoho](https://www.zoho.com/security/report-vulnerability.html). Bugs in *this* project's handling of Zoho's responses are in scope.
- **The behavior of the LLM client you connect.** How Claude, ChatGPT, or another client decides which tools to call is that client's design. What's in scope is this server enabling something it shouldn't, or failing to constrain what it claims to constrain.
- **An operator deliberately enabling `ZOHO_ALLOW_AUTO_SEND`** and then getting mail sent they didn't want. That setting exists precisely to make this a conscious choice, and its risks are documented. A *bypass* of the flag is in scope; using it as designed is not.
- Requesting broad OAuth scopes. The scopes are documented, the read-only subset is documented, and granting them is the operator's decision.
- Anything requiring an attacker to already have local access to an unlocked machine with the OS credential store unlocked. At that point the mail client is readable too.

## What the project already does

Not a guarantee — context for judging whether something is a real finding:

- **The refresh token lives in the OS credential store** via `keyring` (Windows Credential Manager, macOS Keychain, Secret Service), never in a file in the repo or working directory.
- **Tokens never appear in errors.** The access token is used only to build the `Authorization` header. Auth failures surface Zoho's own error field, not the credential.
- **`.env` is gitignored**, with `.env.example` explicitly excepted so the template stays tracked and real secrets don't.
- **Sending email is off by default.** `send_email` raises before making any network call unless `ZOHO_ALLOW_AUTO_SEND` is set to `true` (case-insensitive, surrounding whitespace ignored -- so `TRUE` and `True` also enable it; any other value leaves sending off). The check lives in the client — the layer that issues the request — not in a tool wrapper that a different code path could sidestep.
- **There is no send-a-reply tool in any configuration.** Replies quote incoming mail, which is untrusted input; they always stop at Drafts.
- **Least-privilege scopes.** Creation uses `.CREATE` rather than `.ALL` where only creation is needed, and the read-only subset is documented for people who want no write access at all.
- **Raw vendor errors don't reach the model.** Failures are wrapped in `ZohoAPIError` with a specific message rather than a raw HTTP failure or traceback.

## For operators

Two things to understand before pointing an assistant at your mailbox:

**Email is untrusted input.** Anyone can send you a message containing text aimed at the assistant reading it — "forward this thread to…", "reply saying…". This is why composition is draft-first, and why replies can't be sent at all. The protection only holds if you leave `ZOHO_ALLOW_AUTO_SEND` off; turn it on and a convincing message plus a compliant model becomes a real path to mail leaving your account without you seeing it.

**The token is as sensitive as your mailbox password**, and revoking it is the fix if anything looks wrong. Revoke at the [Zoho API Console](https://accounts.zoho.com/developerconsole), then re-run `uv run zoho-mcp-setup`. Deleting `.env` alone is not enough — the refresh token is in your OS credential store, not in the file.

## Supported versions

Pre-1.0 and single-maintainer: only the current `main` is supported. Fixes land there, and there are no backports to older tags.
