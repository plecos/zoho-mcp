# This project vs. Zoho's own MCP

Zoho ships a first-party MCP offering ([zoho.com/mcp](https://www.zoho.com/mcp/)).
It covers Zoho Mail among ~47 products, so the overlap with this project is real
and the question "why not just use theirs?" is a fair one.

This page answers it. It is not a sales pitch — for several use cases Zoho's is
the better choice, and those are listed too.

**Last checked: July 2026**, against a live Zoho MCP server built from their Mail
Reading & Search template — tool schemas, OAuth scopes, and real authenticated
responses from two endpoints, not just their docs. Their offering is new and moving; if you're
reading this much later, re-verify before trusting it. See [Re-checking this
page](#re-checking-this-page).

---

## What Zoho's MCP is

From Zoho's documentation and the server catalog in their console:

- **Hosted and remote.** You create a server in Zoho's console and it generates a
  unique URL for you; that URL is the endpoint your MCP client calls. Calendar's
  docs describe it as reaching Zoho Calendar APIs on a remote server, over
  JSON-RPC 2.0.
- **Organization-oriented.** OAuth 2.1, with two modes: each user authorizes
  individually, or a Super Admin authorizes a connection once and shares the
  server URL with the team.
- **Broad.** Roughly 47 Zoho services — CRM, Desk, Cliq, Projects, WorkDrive,
  Books, People, Mail, Calendar, and more. It's a bus onto the whole product
  line.
- **API methods as tools.** Calendar's docs describe the server as exposing
  Calendar API methods as tools.
- **Composable, task-scoped bundles.** The console offers ~42 pre-configured
  servers, each a bundle of tools for one job, plus a "Create MCP server" path
  for custom ones. You pick which bundles your server gets.

Zoho Mail is split into three separate bundles: **Mail Reading & Search**, **Mail
Sending & Replies**, and **Mail Organization & Management**. That split matters
in both directions and is discussed under [the send gate](#the-send-gate).

Sources: [Zoho MCP](https://www.zoho.com/mcp/) ·
[supported services](https://www.zoho.com/mcp/services/zoho-services.html) ·
[Mail MCP configuration](https://www.zoho.com/mail/help/mcp/mcp-server-configuration.html) ·
[Calendar MCP getting started](https://www.zoho.com/calendar/help/mcp/getting-started.html)

## What their tools actually look like

Observed by adding the **Mail Reading & Search** bundle to a server in Zoho's
console, which then lists the tools it linked. Twelve of them:

`getMailAccounts` · `getAccountDetails` · `getAllFolders` · `getFolder` ·
`listEmails` · `SearchEmails` · `getMessageDetails` · `getMessageContent` ·
`getOriginalMessage` · `getMessageAttachmentInfo` · `readMessages` ·
`flagMessages`

Three things follow from that list.

### It's a 1:1 mapping of API methods

The tool names are Zoho's REST method names, and the descriptions are their API
reference copy — several still refer to the message id being "passed in the
request URL," which describes an HTTP mechanic the calling model never sees.
`SearchEmails` is PascalCase while the other eleven are camelCase, which is the
kind of seam you get from generated bindings rather than a designed surface.

The parameter schemas confirm it. Arguments are nested under `path_variables` and
`query_params` — the shape of an HTTP request, handed to a model that isn't
making one:

```
SearchEmails(path_variables={accountId}, query_params={searchKey, limit, start})
```

Three consequences worth naming, because each one is work pushed onto the caller:

- **`accountId` is required on nearly every tool**, and the only way to get it is
  `getMailAccounts` — the call documented [below](#a-read-tool-returns-far-more-than-the-mailbox)
  as returning the account holder's phone number and SMTP configuration. Reading
  mail begins by pulling the full account record. Here, the account id comes from
  config and never enters a conversation.
- **Date filtering is a string inside the query**: `fromDate:DD-MMM-YYYY`. The
  model formats the date, and it does not know the mailbox's timezone — so "since
  yesterday" is resolved in whatever timezone the model assumes. This project's
  `days_back` takes an integer and resolves the boundary server-side against the
  live mailbox timezone, precisely so that guess never happens.
- **`searchKey` is mandatory.** There's no "recent mail, no query" path through
  `SearchEmails`.

This is a fair architecture for a bus onto 47 products — arguably the only one
that scales to that many, and generated bindings are how you ship 47 products
rather than 3. It is a different thing from a curated tool surface. The other
tradeoffs are the ones you'd expect: two calls (`getMessageDetails`,
`getMessageContent`) where this project has `get_email`, and vendor parameters
like `includeBlockContent` surfaced for the model to reason about.

### A "Reading & Search" bundle contains two write tools

`readMessages` marks messages read. `flagMessages` sets a flag. Both mutate the
mailbox, and both are in the bundle named for reading.

Keep this proportionate: these are reversible, low-harm mutations, not sending.
Nothing here contradicts the value of the [send gate](#the-send-gate) — the
sending bundle is still separate, and declining it still means no mail leaves.

What it does mean is that **the bundle boundary is a task boundary, not a
read/write boundary**, so "I only added the reading bundle" is not the same
statement as "this server is read-only." If you want a genuinely read-only Zoho
MCP server, verify the linked tool list rather than trusting the bundle name.

For contrast, this project annotates every tool with its actual semantics —
`readOnlyHint=True` for reads, and distinct `_CREATE` / `_UPDATE` / `_DELETE` /
`_MAIL_UPDATE` / `_SEND` annotations for writes, chosen per tool rather than
one-size-fits-all. `mark_as_read` is annotated as the write it is. That's a
convention this project enforces on itself
([CLAUDE.md](../CLAUDE.md#read-only-vs-write-tools)); it's noted here because the
comparison is the whole point of the page, not as a gotcha.

### The OAuth grant is scoped to the product, not the bundle

Every Zoho MCP server publishes its OAuth metadata unauthenticated, at
`/.well-known/oauth-protected-resource` on the server's own host. For a server
built from **Mail Reading & Search and nothing else**, the scopes it requests
are:

```
ZohoMail.messages.READ    ZohoMail.messages.UPDATE    ZohoMail.messages.ALL
ZohoMail.folders.READ     ZohoMail.folders.ALL        ZohoMail.accounts.ALL
ZohoMCP.tool.execute
```

`ZohoMail.messages.ALL` is Zoho's full-access mail scope. It is the same scope
this project requests to turn on its **write** tools
([setup_auth.py](../src/zoho_mcp/setup_auth.py)), and the one the
[README](../README.md#setup) tells you to omit if you want read-only access.
`folders.ALL` and `accounts.ALL` are likewise full-access variants, requested
alongside the `.READ` ones rather than instead of them.

Zoho's consent screen renders those same seven scopes like this, under a heading
that reads only "Mail":

| Consent screen wording | Scope |
| --- | --- |
| To execute the requested tool in Zoho MCP | `ZohoMCP.tool.execute` |
| All read and write operations on mail account | `ZohoMail.accounts.ALL` |
| Update mail related information | `ZohoMail.messages.UPDATE` |
| All read and write operations on mail | `ZohoMail.messages.ALL` |
| All read and write operations on folder | `ZohoMail.folders.ALL` |
| View folder related information | `ZohoMail.folders.READ` |
| View mail | `ZohoMail.messages.READ` |

So the token behind a read-labeled server is a full-mailbox token, and the
consent screen says so — three lines of it begin "All read and write operations."
The disclosure is accurate.

What it doesn't do is *name the consequence*. "All read and write operations on
mail" is the scope this project relies on for composing and sending
([setup_auth.py](../src/zoho_mcp/setup_auth.py) requests no other messages-write
scope, and `send_email` works under it). An operator approving that line for a
server they built and named "Mail Reading & Search" is authorizing outbound mail,
and nothing on the screen uses the word *send*. Nor does the screen identify
which server or template is asking — it says "Mail."

The fair reading, and the limit of the claim: **the bundle exposes no send tool,
so you cannot send through that MCP server.** The exposure isn't the tool
surface, it's the grant sitting behind it — the blast radius if that token is
ever mishandled, and the gap between the capability a user thinks they approved
and the one they did.

This is the sharpest contrast on the page, because it's the one thing a user
can't see from the product UI. Here, scopes track tools: `.CREATE` is requested
where only creation is needed, `.ALL` only where writes exist, and the read-only
subset is [documented and real](../README.md#setup) — drop the `.ALL` and
`.CREATE` scopes and the write tools fail with a scope error rather than silently
having had permission all along.

Credit where it's due on the surrounding machinery: the auth is MCP-spec OAuth
2.1 done properly — PKCE with S256, dynamic client registration, a published
revocation endpoint, and a clean `WWW-Authenticate` challenge pointing at
discovery. The protocol implementation is good. It's the scope granularity that's
coarse.

### Where they cover more

`getOriginalMessage` returns the raw MIME of a message. **This project has no
equivalent** — if you need headers, signatures, or the original encoding, theirs
does something ours doesn't.

They also got the batch shape right: `readMessages` and `flagMessages` both take
one or many ids. That's the same lesson recorded in
[CLAUDE.md](../CLAUDE.md#watch-a-real-client-use-the-tools) — a per-item tool
shape is only correct when the underlying API is per-item, and Zoho's isn't.

## What their output looks like

Verified by calling `getMailAccounts` against a live authenticated server. No
values are reproduced here — only shapes.

### It's the raw Zoho payload

The response is Zoho's own API envelope, re-wrapped in another one:

```
{"data": {"status": {"code": 200, "description": "success"},
          "data": [ ...the account record... ]},
 "status": "success"}
```

Two nested `data` keys and the same status expressed twice. Nothing is reshaped;
the vendor response is passed through with a wrapper added.

### Timestamps are raw epoch milliseconds

`lastLogin`, `accountCreationTime`, `lastPasswordReset`, and
`mailboxCreationTime` all come back as 13-digit integers. No ISO conversion, no
offset applied.

The same response also contains the account's IANA timezone. So the two pieces
needed to render those timestamps correctly are both present, in the same
payload, unjoined — and the model is left to combine them.

That is this project's [don't delegate correctness to the
caller](../CLAUDE.md#dont-delegate-correctness-to-the-caller) rule as a live
specimen. For contrast, `search_emails` here returns:

```
"date": "2026-07-25T19:14:03.525000-07:00"
```

ISO 8601, already in the mailbox's own offset, resolved server-side from the
timezone this project fetches live rather than storing. There is no arithmetic
left for a caller to get wrong.

### A read tool returns far more than the mailbox

A single `getMailAccounts` call placed all of the following into the model's
context: the account holder's full name and gender, their personal phone number
(twice — once under a misspelled key, the same class of quirk catalogued in
[zoho-api-notes.md](zoho-api-notes.md)), every alias address on the account,
two-factor status, the timestamp of the last password reset, storage consumption,
plan and policy identifiers, the admin role, an 18-entry feature-flag map, and
the complete outgoing SMTP configuration — server, port, connection type — for
seven send-as identities.

That is the response to "which account am I operating on."

This project exposes **no account-details tool at all.** The account id lives in
config; the mailbox timezone and outgoing address are fetched by `ZohoClient`,
cached for the process lifetime, and used internally. None of them are ever
returned to the model, because none of them are the model's business.

The general principle, and the reason this sits in a comparison rather than a
bug report: a 1:1 API mapping inherits the API's disclosure surface. REST
endpoints were designed for application code that ignores fields it doesn't need.
A tool result is different — everything in it enters a context window, and some
of it is the kind of thing you would not paste into a chat on purpose.

### The email record, side by side

`SearchEmails` and `search_emails`, same mailbox, same window, same messages.
Field names and types are verbatim. Values are redacted, except the two
timestamps and the date derived from them — those are real, so the offset
arithmetic below can be checked rather than taken on faith.

**Theirs** — 20 fields per message:

```
{"summary": "…",              "sentDateInGMT": "1785057240000",
 "receivedTime": "1785032043525",  "calendarType": 0,
 "subject": "…",              "messageId": "…",
 "flagid": "flag_not_set",    "status2": "0",
 "priority": "3",             "hasInline": "true",
 "toAddress": "&lt;user@example.com&gt;",
 "ccAddress": "Not Provided", "folderId": "…",
 "hasAttachment": "0",        "size": "24825",
 "labelId": ["…"],            "sender": "…",
 "fromAddress": "…",          "status": "0"}
```

**Ours** — 7:

```
{"id": "…", "from": "…", "subject": "…",
 "date": "2026-07-25T19:14:03.525000-07:00",
 "snippet": "…", "folder_id": "…", "read": false}
```

Six things in that left-hand column are worth naming:

- **Two epoch-millisecond timestamps, both as strings.** `receivedTime` is the
  value this project converts; the ISO string above is that same instant rendered
  in the mailbox's offset.
- **`sentDateInGMT` is not GMT** — the quirk already recorded in
  [zoho-api-notes.md](zoho-api-notes.md), now demonstrable arithmetically. On a
  machine-generated notification, where send and receipt are seconds apart, it
  sits almost exactly one UTC offset ahead of `receivedTime` (here ≈ 7h, the
  mailbox's PDT offset, rounded to the minute). A model handed that field will
  read local time as GMT. It is passed through under its misleading name.
- **Booleans and numbers are strings.** `hasInline` is `"true"`, `hasAttachment`
  is `"0"`, `size` and `priority` are quoted. `calendarType` is the one genuine
  integer.
- **Read status is `status`: `"0"` / `"1"`** — a stringly-typed flag whose key
  collides with two other `status` keys in the same response (the envelope's
  `"success"` and the nested status object's `code`). Three meanings, one name,
  one payload. There is also a `status2`.
- **HTML entities are left encoded.** Addresses arrive as `&lt;…&gt;` and
  `&quot;…&quot;`, so the model sees markup where the address should be.
- **Empty fields use a sentinel string.** An absent CC is the literal text
  `"Not Provided"`, not `null` or an omitted key — so any caller has to know that
  string means nothing.

Field sets also vary between records in a single result set: sent items carry
`threadId`, `threadCount`, `toAddr`, and `mailDeliveryStatus`; received ones
don't. A caller cannot assume a stable schema across the array.

**And a behavioral difference, not just a shape one:** their result set included a
message from the Sent folder. `search_emails` excludes Sent, Drafts, and
Templates by default — documented in the [README](../README.md#tools) — because a
general mailbox search that surfaces your own outbound mail is almost never what
was meant. Theirs returns it, and the folder id is the only signal.

## What we still could not verify

Stated plainly, because this project's first rule is that documentation isn't
evidence — and that rule doesn't get suspended when the docs belong to someone
else.

The tool list, OAuth scopes, and output shapes above are confirmed against a live
authenticated server, across two endpoints. What is **not** confirmed:

- **How they surface errors.** No failing call has been observed, so whether a
  Zoho error reaches the model raw — the thing this project's
  [error handling](../CLAUDE.md#error-handling) rules exist to prevent — is
  unknown.
- **The other bundles' tool lists and scopes.** Only Mail Reading & Search has
  been inspected. The Sending and Organization bundles may differ in ways that
  cut either direction.
- **Whether any of this is stable.** Two endpoints on one template, on one day.

Nothing here is a claim about Zoho MCP as a whole. It's a claim about what one
template returned when asked.

---

## How the two differ

### Where your mail goes

Zoho's server is hosted infrastructure. Your prompts, tool arguments, and the
returned mail and calendar content traverse a Zoho-operated MCP endpoint in
addition to the Zoho APIs they already touch.

This server runs on your machine over stdio. There is no listening socket, no
URL, and no third-party relay between the client and Zoho's APIs. The only
network calls are the ones to Zoho's REST endpoints.

Neither is "more secure" in the abstract. They're different trust boundaries, and
which one you want depends on whose infrastructure you're comfortable with.

### Tenancy

| | Zoho MCP | zoho-mcp |
| --- | --- | --- |
| Users | Team/org, shareable URL, admin-authorized connections | One account, one OAuth token |
| Setup | Console UI | `uv run zoho-mcp-setup` |
| Ops burden | Zoho's | Yours |

If you need several people on a shared server, this project simply doesn't do
that, and won't. See [Status](../README.md#status).

### Breadth vs. depth

Zoho spans ~47 products. This project covers Mail, Calendar, Contacts, Tasks,
Notes, Bookmarks, Groups, and Resource Booking — and nothing else, ever.

That's a real trade. If your workflow is "read the deal in CRM, then draft the
follow-up," theirs does it and this doesn't.

Where the trade goes the other way, the console catalog is more informative than
the services list, because it says what each bundle covers:

- **Zoho Contacts — the address book — has no bundle.** The catalog does have
  "Contact Hub & Merging" and "Desk Contacts & Accounts," but those are *CRM* and
  *Desk* contacts: different products, different records, different scope
  families. This project treats Zoho Contacts as its own client
  (`contacts_client.py`) for exactly that reason, and handles the Personal vs.
  Organization pool split that makes a `contact_id` non-unique.
- **Mail's Tasks, Notes, and Bookmarks have no bundle.** "Notes & Contextual
  Collaboration" is CRM notes; "Project & Task Management" is Zoho Projects.
  Neither is the Tasks/Notes/Bookmarks feature set inside Zoho Mail.
- **Zoho Calendar has no pre-configured bundle**, despite having its own MCP
  documentation. Zoho Bookings does, but that's appointment scheduling, not your
  calendar.

Read that carefully: absence from the pre-configured catalog is **not** absence
from the platform. There's a "Create MCP server" path for custom servers, and
Calendar's docs exist, so these are plausibly reachable by building one. The
honest claim is narrower — no ready-made bundle covers them, so reaching them is
your work rather than a checkbox.

### The send gate

This is the sharpest difference between the two, and both sides of it are worth
stating fairly.

**Zoho's model gates at configuration time.** Sending lives in its own bundle,
Mail Sending & Replies, described in their catalog as composing and sending new
mail, replying to existing threads, and handling outgoing attachments. Reading
and organizing live in two separate bundles. So an operator who never adds the
sending bundle gets a mailbox server that cannot send — a real and useful
affordance, and a genuine point in their favor.

Two caveats on that gate, both from inspecting a real server:

- Bundles are scoped by *task*, not by read/write. The Reading & Search bundle
  ships [two write tools](#a-reading--search-bundle-contains-two-write-tools).
- The OAuth grant behind that bundle [includes the full-access mail
  scope](#the-oauth-grant-is-scoped-to-the-product-not-the-bundle) — the one that
  authorizes composition and sending.

The sending separation still holds at the tool layer, which is what stops mail
leaving: no send tool, no send. But "I declined the sending bundle" constrains
the tools, not the token.

**This project gates at request time, and omits replies entirely.**

- `send_email` refuses unless the operator sets `ZOHO_ALLOW_AUTO_SEND=true`, and
  fails **before making any network call**.
- The check lives in `ZohoClient` — the layer that issues the request — not in a
  tool wrapper that another code path could sidestep.
- **There is no send-a-reply tool in any configuration.** Replies quote incoming
  mail, incoming mail is untrusted input, and a message in your mailbox can
  contain text aimed at talking an assistant into sending something. Replies stop
  at Drafts, always.

The practical difference is what happens once you *do* want the assistant to
compose. Adding Zoho's sending bundle appears to bring send-new and send-reply
together; there's no published way to take composition without reply-sending.
Here, `create_draft` and `reply_draft` always work and always land in Drafts,
sending new mail is a separate opt-in, and reply-sending isn't available at any
setting.

Zoho's Mail API also makes sending the default and drafting the opt-in flag (see
[zoho-api-notes.md](zoho-api-notes.md)); this project inverts that and pins the
inversion with tests.

None of this is a knock on Zoho. A general-purpose bus onto 47 products can't be
opinionated about one mailbox's threat model. This project only has to be.

Full reasoning: [SECURITY.md](../SECURITY.md).

### Correctness done once, in tested code

The rule this project runs on is that math a caller could get wrong belongs in
the server ([CLAUDE.md](../CLAUDE.md#dont-delegate-correctness-to-the-caller)).
It exists because of a real failure: one session converted UTC to local time
correctly and a later session, same server and same data, displayed the raw UTC
digits mislabeled as local.

So here, before anything reaches the model:

- Every date and time is already in the **mailbox's own local offset**, fetched
  live per client instance rather than stored in config where it would go stale.
- Epoch strings become ISO 8601; HTML bodies become plain text — in exactly one
  place, which every tool calls through.
- Bounds Zoho accepts silently but truncates invisibly are enforced locally.
- `get_freebusy` raises on an unshared calendar rather than reporting a falsely
  empty schedule.
- `get_event` withholds start/end times, because Zoho's single-event endpoint
  returns wrong dates for some recurring occurrences.

Whether Zoho's hosted server does any of this, we don't know. What's claimed here
is only that this one does, and that it's tested.

### Verified against a live account

Every tool here has run against a real Zoho account.
[zoho-api-notes.md](zoho-api-notes.md) is the residue: fields documented optional
that are required, a mandatory field with a typo in its name, endpoints
documented at URLs that 404, field names that contradict their contents.

That document is itself a differentiator. Whatever you build on — this, Zoho's,
or your own — it's the part that took the longest and it's freely readable.

---

## When to use Zoho's instead

Genuinely, not as a hedge:

- **You need products this doesn't cover.** CRM, Desk, Projects, Books, WorkDrive
  — not here, not planned.
- **You need more than one user.** Shared URL, admin-authorized connections, no
  per-person setup.
- **You don't want to run anything.** No Python, no `uv`, no local process, no
  token in your OS credential store.
- **You want vendor support.** This is a single-maintainer project with no SLA.

Use this one when you want a local, single-user server that's opinionated about
mail safety, hands the model data that needs no further conversion, and is small
enough to read end to end.

They also aren't exclusive. Nothing stops you running both and pointing your
client at each.

---

## Re-checking this page

Zoho's docs will move. To re-verify:

1. Re-read the four source links above.
2. Open the server catalog in Zoho's MCP console — it's more current and more
   specific than the marketing pages, and it's where the Mail bundle split and
   the Contacts/Calendar gaps came from.
3. Re-check the linked tool list for Mail Reading & Search — the write tools in
   it may be a deliberate grouping or an oversight, and either way it's the kind
   of thing that changes without an announcement.
4. Re-fetch `/.well-known/oauth-protected-resource` on the server's host and
   diff the scopes against the table above — it's public, needs no auth, and is
   the fastest check on this page. The consent screen is the second opinion.
5. Capture a `SearchEmails` response, the one gap left in [what we still could
   not verify](#what-we-still-could-not-verify).
6. Check whether bundles have appeared for Zoho Contacts, Zoho Calendar, or
   Mail's Tasks/Notes/Bookmarks.
7. Update the "Last checked" date whether or not anything changed — a stale date
   is more useful than a confident one that's wrong.
