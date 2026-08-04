# Zoho API field notes

Everything here was confirmed against a live Zoho account, not taken from the
documentation. It's recorded because a surprising amount of it contradicts
Zoho's own docs, and because the same surprises will bite anyone else building
against these APIs.

Each entry says what the docs claim, what actually happens, and what this
codebase does about it. If you're changing code that touches one of these,
read the entry first — several of them look like bugs until you know why.

For the *general* engineering lessons drawn from these (verify live, demand
verbatim quotes, etc.), see [CLAUDE.md](../CLAUDE.md).

---

## Cross-cutting

### Every Zoho product uses a different write convention

Three products, three transports, same company and same OAuth token:

| API | How the payload is sent |
| --- | --- |
| Calendar (create/update/delete event) | JSON-encoded `eventdata` **query parameter** |
| Mail (message state, composition) | JSON **request body** |
| Tasks / Notes / Bookmarks (create) | JSON **request body** |

Never assume one Zoho product's convention holds for another. This is why
`_zoho_authenticated_request` supports both `params` and `json_body`.

### Read and write schemas are not the same schema

Several endpoints return a field on read that they reject (or type differently)
on write:

- **Calendar `notifyType`** — present in a `GET` event, rejected on `PUT` with
  `400 PATTERN_NOT_MATCHED`. It is *not* the write-side `notify_attendee`
  despite describing the same concept.
- **Notes `color`** — read returns the string `"-1"`; create requires an
  **int**.

Consequence: never echo a GET response back as a write payload. `update_event`
builds its payload from `_EVENT_WRITABLE_FIELDS`, an explicit allowlist, rather
than stripping known-bad keys from the raw response — a blocklist only protects
against the fields you've already discovered.

### Invalid input is often accepted silently rather than rejected

- **Notes/Bookmarks `limit`** — documented max 399, but `limit=10000` returns
  `200`. An over-max request isn't an error, it's an invisible truncation the
  caller cannot distinguish from "that's all the data". Bounds are enforced
  locally for this reason.
- **Calendar free/busy** — a calendar that hasn't opted into sharing returns
  `{"fb_not_enabled": true}` with a `200`, not an error. Treating that as "zero
  busy slots" would be indistinguishable from "confirmed free", so
  `get_freebusy` raises instead.

---

## Mail — reading

### `sentDateInGMT` is not GMT

Despite the name, it was consistently off by exactly the account's own UTC
offset (~7h) across five unrelated senders — a marketing platform, a utility,
a personal calendar invite, a bank, a courier. That rules out a single sender's
clock being wrong. `receivedTime`, Zoho's server-side receipt timestamp, is the
reliable field and is what `search_emails` uses.

### Subjects and snippets arrive with undecoded HTML entities

Not an edge case: in a sample of 200 real emails, literal `&#39;`-style
entities appeared in ~10% of subjects and ~36% of snippets. Both fields get
`html.unescape()`.

### `status` encodes read/unread as a string

`"1"` is read, `"0"` is unread (confirmed by sending a fresh unopened email and
comparing). Any other value is treated as unread — the safer failure mode is
"shows as unread when it isn't" over hiding real unread mail.

### Search and List are different APIs with different capabilities

`GET .../messages/search` has **no** read/unread filter and **no** pagination —
it returns the top N by recency, full stop. Older unread mail on a busy day is
simply invisible to it.

`GET .../messages/view` (the List Emails API, documented at a different URL)
has a real `status` filter (`read`/`unread`/`all`) *and* `start`/`limit`
pagination. `list_emails` wraps this; `search_emails` wraps Search. Use List
for anything that must enumerate *every* match.

### Nothing in the Mail API reports an unread total

There is no unread (or total) message count anywhere the obvious places would
put one — verified live against a 26-folder account, not inferred from the
docs:

- `GET /accounts/{id}/folders` rows carry exactly `HIDE`, `URI`, `VW`,
  `archivePolicyId`, `folderIcon`, `folderId`, `folderName`, `folderType`,
  `imapAccess`, `isArchived`, `path`, and (per row) `parentFolderId` /
  `previousFolderId`. No `unreadCount`, no `totalCount`, nothing count-shaped.
- `GET /accounts/{id}/folders/{folderId}` returns the same key set for one
  folder. The detail endpoint carries nothing extra.
- The response envelope holds only `data` and `status`, and `status` is just
  `{"code", "description"}` — no paging block, no total, on either the folders
  or the `messages/view` endpoint.

So a mailbox-wide unread count can only be obtained by paging
`list_emails(status="unread")` and summing each page's `count`. That's the
reason the count is computed server-side per page and `has_more` drives the
paging: there's no authoritative total to check the sum against, which makes
an LLM's own tally of the returned rows the *only* other option and the worse
one.

### Drafts are invisible to the search API

`in:Drafts` returns an empty list even when drafts demonstrably exist. To find
drafts, use `list_emails(folder_id=...)` with the Drafts folder id from
`list_folders`.

### Two folder fields look like parents; only `parentFolderId` is one

`previousFolderId` is not a parent pointer. Despite the name, it's a
display-order "previous sibling" pointer — folders form a linked list, not a
tree, in that field. It's excluded from the normalized shape because it invites
exactly the wrong inference.

`parentFolderId` **is** the parent, and is exposed as `parent_id`. Verified
against a live 26-folder mailbox nesting three levels deep: it appeared on
exactly the 16 non-top-level folders, was absent on all 10 top-level ones,
agreed with `path` in all 26 cases, and never pointed at an id outside the
response. Absent means "no parent" — there is no separate null or sentinel.

The trap worth naming: on a subfolder both keys are usually present and
**often hold the same id**, because a subfolder's previous sibling is
frequently also its parent in Zoho's ordering. So reading `previousFolderId`
as a parent — or falling back to it when `parentFolderId` is missing — looks
correct on most rows and silently builds a wrong tree on the rest. The
fallback is pinned shut by
`test_normalize_folder_never_reads_a_parent_out_of_previous_folder_id`.

`path` is still the signal to *display* (it needs no lookup); `parent_id` is
for walking the tree by id. This one was missed originally because the earlier
note only established what `previousFolderId` wasn't, and never asked whether
some other key was the thing it had been mistaken for.

### Custom folders all report `folderType: "Inbox"`

Every user-created folder, including mail-rule destinations and nested
subfolders, reports type `Inbox`. That's what makes excluding
Sent/Drafts/Templates *by type* safe — it can never accidentally catch a user's
own folder.

### "Notification" is a real folder *and* a label

Zoho files certain mail into a genuine `/Notification` folder (`folderType:
"Inbox"`, like any custom folder) and *also* tags it with a matching label.
Both `in:Notification` and `label:Notification` work.

### The key set varies message to message, on both list endpoints

Not just between endpoints — between *rows of the same response*. Across 60
messages from `/messages/view`, five distinct key sets appeared: `toAddress`
was missing on one, `labelId` on 27, and `threadId`/`threadCount` were
present on only two. `/messages/search` was the same story, plus
`toAddr` and `mailDeliveryStatus` on a single row and nowhere in `view`.

So every field beyond the core has to tolerate absence.
`normalize_email_summary` defaults them rather than raising, and only the
fields a record is unusable without still raise.

### An empty address field is the literal text `Not Provided`

Not `null`, not `""`, not an absent key. `ccAddress` was the string
`"Not Provided"` on 116 of 120 real messages. Passed through, a model
reports a recipient by that name.

`toAddress`/`ccAddress` are also HTML-entity-encoded (`&lt;a@b.com&gt;`)
and comma-joined when there's more than one.

### `sender` is a display name, not an address

Despite sitting next to `fromAddress` and looking like a peer. On 25 of 60
real messages it contained no `@` at all while `fromAddress` did; on the
other 35 it simply repeated `fromAddress` verbatim. It's the display name
when there is one, and a copy of the address when there isn't — surfaced
here as `from_name`, blank when it would only duplicate.

### `priority` is X-Priority, confirmed against the `Importance` header

Lower means more important. Established by sending a high-importance message
and reading its own source back:

| `priority` | `Importance` header on the same message |
| --- | --- |
| `"2"` | `High` |
| `"3"` | `Medium` |
| `"4"` | `Low` |

So Zoho's numeric field follows the X-Priority convention, and `"3"` is the
default — it was `"3"` on 48 of 50 real messages. Those three are the range
Zoho's own compose UI can produce. `1` and `5` are highest and lowest under
the same convention but have **not** been observed here; they'd have to come
from a sender using another client.

This was recorded as unverifiable for weeks, because a real mailbox with no
prioritized mail in it gives you a column of `"3"` and no way to read it. The
fix was to create the data, then check the answer against the message's own
headers rather than against a guess.

### `flagid` is a flag *type name*, not a boolean

`flag_not_set` when unflagged; a name otherwise. All three of Zoho's own flag
types have now been set on real messages, and every one is lowercase with **no
separator**:

| Zoho's UI | `flagid` |
| --- | --- |
| Important | `important` |
| Follow Up | `followup` |
| Info | `info` |

That second one is why `normalize_email_summary` passes any name through
unchanged rather than matching against a list. The first draft of its test
guessed `follow_up`, and the real value has no underscore — so an enumeration
built from that guess would have silently dropped a flag the user had actually
set. These three appear to be the complete set Zoho's UI offers, but a
message from another client could carry something else, so names still pass
through rather than being validated against the table.

### The thread fields still could not be verified

`threadId`/`threadCount` appeared on 2 of 60 messages, reporting
`threadCount: "0"` with `threadId == messageId`. That distinguishes nothing,
so neither is surfaced. Unlike flag and priority, this one has no obvious
test to construct — it would need a genuinely threaded conversation.

### `originalmessage` is account-scoped and types its id differently

`GET /accounts/{accountId}/messages/{messageId}/originalmessage` takes **no
`folderId`**, unlike every other per-message endpoint.

The response is JSON — `data.content` holds the entire RFC 822 source as one
string — and `data.messageId` comes back as an **int**, where every other
endpoint sends the same id as a string.

Sizes are the reason `get_email_source` parses rather than forwards: three
ordinary messages measured 28,469, 41,142, and 82,201 characters of source.

### Nothing in the attachment APIs tells you an attachment's type

Two endpoints are involved and neither carries a media type.

`.../messages/{id}/attachmentinfo` returns exactly three fields per
attachment — `attachmentId`, `attachmentName`, `attachmentSize`. Verified
across 180 real messages: no fourth key ever appeared.

`.../messages/{id}/attachments/{attachmentId}` returns the content as a raw
byte stream, and its `Content-Type` is **always**
`application/octet-stream;charset=UTF-8`. Verified against a gzip, a PNG, a
GIF, a PDF, and an `.ics` — all five reported the same header, and the
`charset=UTF-8` on a PNG is meaningless. The header is not a type signal.

So the filename extension is the only *hint* available. `get_attachment`
uses it for a best-effort `media_type` but decides text-vs-binary by
actually decoding the bytes, since the extension can lie in both directions.

### The attachment filename lives in `Content-Disposition`, oddly formatted

Two things about it. The header is written `attachment; filename = <name>`
— with spaces around the `=`, which a plain `filename=` split misses. And
the name is percent-encoded, so `report!.csv` arrives as `report%21.csv`.

Unrelated to the Resource Booking percent-encoding [noted
below](#percent-encoded-names-were-a-data-entry-artifact-not-an-api-quirk),
which was bad data rather than transport encoding. This one is real
encoding and does need decoding.

---

## Mail — writing

### Sending and saving a draft are the same request

`POST .../messages` with the same body does both. The **only** difference is
`"mode": "draft"`. Omitting it doesn't error — it delivers mail to a real
person.

This is the most dangerous default in any of these APIs, and the reason
composition is structured so the safe path is not something anyone has to
remember: `create_draft`/`reply_draft` pass an explicit `as_draft=True` into a
shared builder, and tests assert `sent["mode"] == "draft"`. **Do not remove
those assertions** — they're the guard against a refactor silently mailing
strangers.

### One endpoint handles all message-state changes

`PUT .../updatemessage` covers mark read/unread, move, and label add/remove,
differing only by `mode` plus mode-specific extras (`destfolderId`,
`labelId`). It accepts an **array** of `messageId`s, so all five tools batch.

Zoho's docs are internally inconsistent on `labelId`'s shape — the apply sample
shows an array, the remove sample a single value. An array works for both.

#### Its success response says nothing about the messages

Verified live, 2026-08-04, by issuing each case against a real mailbox and
reading the raw body (the docs describe the request only, and say nothing about
the response):

| request | HTTP | body |
| --- | --- | --- |
| one real id, `markAsRead` | 200 | `{"status":{"code":200,"description":"success"}}` |
| two real ids | 200 | *byte-identical to above* |
| **one nonexistent id** | **200** | *byte-identical to above* |
| **one real + one nonexistent** | **200** | *byte-identical to above* |
| **`"messageId": []`** | **200** | *byte-identical to above* |
| `"mode": "markAsBogus"` | 400 | `{"status":{"code":400,…},"data":{"moreInfo":"Invalid mode"}}` |
| `moveMessage` to a nonexistent folder | 500 | `{"status":{"code":500,…},"data":{"moreInfo":"An internal error occurred"}}` |

Two consequences, and they pull in opposite directions:

- **The 200 body is a constant.** It is the same bytes for one message as for
  fifty, so there is nothing in it to parse, count, or report. Reading it would
  add a dependency on a field that carries no information — the case for
  discarding it, which `_update_message` does.
- **Unknown ids are accepted silently**, in exactly the shape this vendor's
  other endpoints do. A batch that is entirely garbage is indistinguishable
  from one that is entirely real, so `mark_as_read` **cannot confirm any
  individual message changed state** — only that Zoho accepted the request.
  An empty array is likewise a "success", which is why the client's own
  `message_ids must contain at least one message id` check has to stay: Zoho
  will not raise that for us.

The mode itself *is* validated (400, distinct `moreInfo`), so a wrong `mode`
fails loudly while a wrong `messageId` does not. This is the general pattern
here: vocabulary is checked, referents are not.

Consequently the write tools return the ids they submitted, not a
confirmation — see `_update_message` and the `counted()` envelopes in
`tools/mail.py`. Verifying that a message really changed state needs a separate
read; there is no per-id result to be had from this endpoint.

### Forwarding must go through `action=forward`, not a recomposed body

`POST .../messages/{id}` takes `action`: `reply`, `replyall`, `forward`. Zoho
builds the quoted original server-side from the stored message, so the body we
send carries only the forwarder's added note.

The reason this matters more than it looks: the obvious-seeming alternative —
`get_email` then `create_draft` — is silently lossy. `get_email` runs the body
through `normalize_email_content`, which flattens HTML to plain text, so a
forward rebuilt from it arrives stripped of formatting, inline images and
attachments. That is a real reported bug, not a hypothetical: an assistant
asked to forward a message did exactly this, because no forward tool existed.
**Never round-trip a body through this server to forward it.**

**`action=forward` is accepted vocabulary and fails anyway.** Verified live,
2026-07-27: every `action=forward` request returns a content-free
`500 Internal Error`, while `reply`/`replyall` succeed on the same message with
the same body shape, every time. It is not a wrong-field problem:

- `forward` passes the action pattern check that rejects `Forward`, `FORWARD`,
  and `bogusvalue` with `400 PATTERN_NOT_MATCHED`, so the value is in Zoho's
  enum.
- `toAddress` is recognized (no `EXTRA_KEY_FOUND_IN_JSON`), and Zoho's Mail360
  docs state it "becomes mandatory when the value of the action is set to
  forward". Sending it changes nothing.
- Still 500 with `attachments: []`, with real attachment descriptors, with
  `subject`, `mailFormat`, `encoding`, `askReceipt`, `isSchedule`, on messages
  with and without attachments.

Key-probing technique worth reusing: this endpoint emits
`EXTRA_KEY_FOUND_IN_JSON` for unrecognized keys, but the **action pattern check
runs first**, so a bogus action masks it. `action=forward` is the ideal probe
vehicle instead — it passes the pattern check, always fails internally, and so
never creates a draft while still exercising key validation. That mapped the
accepted vocabulary (`toAddress`, `ccAddress`, `bccAddress`, `subject`,
`attachments`, `inReplyTo`, `refHeader`, `priority`, `mailFormat`, `encoding`,
`askReceipt`) and the attachment object's inner keys (`storeName`,
`attachmentName`, `attachmentPath` recognized; `attachmentId` not).

### The web client doesn't use `action=forward` either

Ground truth from Zoho Mail's own network traffic: forwarding posts
form-urlencoded to the internal **`/zm/send.do`**, not the public REST API,
carrying `accId`, `mymId` (the original message id), `fwdInlineMode=7`,
`originalMode=draft`, `from`/`to`/`cc`/`bcc`/`subject`, and a `content` field
holding **the entire forward body assembled in the browser** — the
`============ Forwarded message ============` header block plus the original
inside `<blockquote id="blockquote_zmail">`. That endpoint authenticates with
session cookies and an `x-zcsrf-token`, so it is unreachable from an OAuth
client and is not a contract to depend on.

The lesson for our own design: **assembling the forward body ourselves is not a
workaround, it is what the vendor's own client does.** The server-side path
exists in the API but is broken.

### Inline images survive, and the rewrite that "fixed" them was a no-op

The web client rewrites inline images to absolute
`https://us4-zmud.zoho.com/zm/ImageDisplay?...` URLs, whereas the content API
returns them **relative** (`/mail/ImageDisplay?na=...&mode=inline`). That looked
like a bug to compensate for, so `forward_draft` briefly absolutized them.

Three measurements, in the order that matters:

1. Post a body with absolute `mail.zoho.com/mail/ImageDisplay` srcs, read the
   draft back — Zoho has **re-relativized all three**. It normalizes its own
   host on store, so the rewrite never reached the wire.
2. Send that draft for real and read the received message's RFC 822 source —
   it is `multipart/related` with three base64 parts (`image/png`,
   `image/jpeg` ×2) carrying the **original message's** Content-IDs
   (`<23abc@pc27>` and friends). Zoho resolves the references into real image
   parts at send time.
3. So inline images survive a forward end-to-end, and the absolutizing was
   work the vendor undid. It's gone; the original's markup now passes through
   completely untouched.

The general lesson, which cost a function and its tests: **a vendor value that
looks wrong may be the form that vendor resolves correctly later.** Before
writing code to correct one, check whether anything downstream already handles
it — here, "relative src" was never broken, it was just not yet resolved.

### Attachments forward by round-tripping the bytes

`POST /accounts/{id}/messages/attachments?uploadType=multipart&fileName=<name>`
with the file as multipart form data returns exactly the descriptor the compose
body wants:

```json
{"storeName": "709548548", "attachmentName": "Invoice.pdf",
 "attachmentPath": "/Mail/3a087e776579a06da9f7e-Invoice.pdf"}
```

Pass those objects as compose's `attachments` array and they arrive at their
original byte sizes. So forwarding with attachments is just: list them, download
each, upload each, attach the descriptors.

`uploadType=multipart` is **required, and its absence doesn't look like a
missing parameter**. Omit it and Zoho answers `400` with "The file was not
attached as the file size was detected as 0 bytes" — an error about your file,
for a request whose file was fine. `uploadType=raw` is rejected by the pattern
check, so multipart is the only form.

The first version of `forward_draft` shipped documentation saying attachments
"are not carried", on the strength of the web client using an internal
`attach.do` for its uploads. That was the wrong inference: the *web client's*
endpoint being unreachable said nothing about whether a public one existed, and
one existed. **Do not promote "I didn't find it" to "it isn't there"** — this
codebase's own rule about looking for a different endpoint applies just as much
after you think you've finished.

### What forwarding actually costs, as shipped

Verified live 2026-07-27, end to end including one real send: markup, tables,
links, the quote block, the `Fwd:` subject, file attachments at their original
byte sizes, and inline images as proper MIME parts all survive. Nothing about a
forward is known to be lossy.

### `fromAddress` comes from `primaryEmailAddress`

Required on every send/draft. It's read live from the accounts endpoint rather
than stored in config, since the account's primary address is a mutable
setting.

---

## Calendar

### `recurrenceid` returns wrong dates for all-day yearly events

Fetching a specific occurrence of a recurring event via `GET .../events/{uid}?
recurrenceid=...` works correctly for a timed weekly event. For an **all-day
yearly** event it returns the right start but an end one day late — `duration`
comes back as 2 days for a genuinely 1-day event and `multiday` flips to
`true`. The same occurrence's dates from `list_events` are correct.

Rather than compensate for a field that's wrong in a way we don't fully
understand, `get_event` doesn't request or return `start`/`end` at all. Timing
comes from `list_events`; `get_event` supplies what `list_events` can't
(organizer, full attendee list, location, description, recurrence rule).

### `list_events` can return only your own attendee entry

For a recurring occurrence, the attendee list may contain just the caller, not
every invitee. That asymmetry is why `get_event` exists.

### Event timestamps come in three shapes

Not the single documented shape:

- `yyyyMMdd` — bare date, all-day events, no time or zone
- `yyyyMMddTHHmmssZ`
- `yyyyMMddTHHmmss±HHMM` — numeric offset

They're nested under `dateandtime.start`/`.end`, not at the top level.

### Update is a full replace and needs the current `etag`

Any writable field not resent is deleted. `update_event` therefore does a
read-modify-write. Delete also requires the current `etag`.

### Free/busy sharing is per-calendar, not account-wide

Settings → Calendar → My Calendars → *a specific calendar's* Details tab →
"include in my Free/Busy sharing". Looking for a single account-level toggle
wastes time; there isn't one.

### Resource free/busy appears unavailable

`/resources/<id>/freebusy` returns 404 for every resource identifier tried,
even with calendar sharing enabled. Not exposed as a tool. Unclear whether it
needs additional account-level setup.

---

## Tasks, Notes, Bookmarks

These three are Zoho Mail features (`/api/tasks`, `/api/notes`, `/api/links`),
sharing Mail's domain and OAuth scope family rather than being separate
products.

### Note creation requires `color`, which the docs call optional

`POST /api/notes/me` with only the documented-mandatory `content` returns
`404 {"description": "Invalid Input"}` — no field name, no hint. Adding an
integer `color` creates the note. Every create sends `-1` (Zoho's own "no
colour" value, as carried by notes made in the web UI).

The recognized key vocabulary is exactly `content`, `title`, `color`,
`bookId`, `tagId`, `colorHex` — established by probing, since Zoho returns a
*different* error (`EXTRA_KEY_FOUND_IN_JSON`) for unrecognized keys. That
difference is a useful oracle: it separates "wrong field name" from "right
name, wrong value".

### The bookmark docs list a field spelled `tiltle`

The mandatory-field list says `tiltle`; the sample body on the same page says
`title`. `title` is correct.

### `isFavorite` is a bool on Notes and a string on Bookmarks

Two sibling endpoints, same field name, same base URL, different type: Notes
returns `false`, Bookmarks returns `"false"`. `normalize_bookmark` compares it
as a string for this reason. Matching docs are not evidence of matching
behavior between these two.

### `after` is an offset, not a cursor

Documented only as "specifies from which retrieval has to be done", which reads
like a cursor — and `entityId` values look like plausible cursors. Passing a
real `entityId` returns a bare `500 Internal Error`; passing a plain integer
works. Note the failure mode is a generic 500, indistinguishable from an
outage.

### `isPrev` is a sort-order flag, not a paging direction

Documented only as "ascending or descending order based on created time",
without saying which value is which. Verified separately on both endpoints:

- absent — identical to `isPrev=false`, newest first
- `isPrev=true` — oldest first (exact reverse)

`after` offsets *within* whichever order is selected. Exposed as
`oldest_first`, sent only when true so the default request is unchanged.

### Bookmarks have no timestamps at all

No created or modified fields. Ordering is the only way to reason about a
bookmark's age — which is what makes `oldest_first` more than a convenience
here.

### No server-side filters exist for these

Tasks has no filter for status, priority, due date, or assignee; Notes and
Bookmarks have none for favorites, colour, or collection. Only pagination.
Filtering happens on returned fields. (Checked deliberately, so the question
doesn't get reopened.)

### Task `priority` casing is normalized server-side

`"high"` is stored and returned as `"High"`. Round-tripping the value is not
case-preserving.

### Task `dueDate` format is unverified

Never seen populated on the account this was built against. Zoho's *request*
sample shows `DD/MM/YYYY`, but the response format is unconfirmed, so it's
passed through as an opaque string rather than parsed under an assumption.

### Task timestamps are already ISO 8601

Unlike Mail (epoch-millisecond strings) and Calendar (custom format), Tasks
returns proper ISO 8601 with a real UTC offset. No conversion needed. Notes,
despite being a sibling feature, uses epoch-millisecond strings like Mail.

### Notes and Bookmarks give no paging signal

No `has_more`, no total. Getting back fewer results than `limit` is the only
indication you've reached the end.

### Cross-group task views need `action=view` at a trailing-slash URL

`GET /api/tasks/` (trailing slash required) with `action=view` and
`view=assignedtome|createdbyme`. Zoho's API index lists these at `/api/tasks`,
which returns `404 URL_RULE_NOT_CONFIGURED`. `assignedbyme` looks like an
obvious third value but 400s with `PATTERN_NOT_MATCHED`.

On an account with no groups, both views return exactly the same tasks as the
personal list.

---

## Groups

### A group is one entity shared across all three services

Not a per-service thing. A single group is returned by `/tasks/groups`,
`/notes/groups`, *and* `/links/groups` under the same id — **including the
services where it holds zero items**. These endpoints list "groups you belong
to", not "groups with items here".

So the same `group_id` works for `list_tasks`/`list_notes`/`list_bookmarks`,
and a group can legitimately have notes but no tasks. `list_groups` merges by
id; an earlier per-service design reported one real group as three.

### The three group endpoints disagree on shape

| Service | Container | Id field | Id type |
| --- | --- | --- | --- |
| Tasks | `data.groups` | `id` | **int** |
| Notes | `data` | `groupId` | **string** |
| Bookmarks | `data` | `groupId` | **string** |

Same group, different key *and* different type. Ids are coerced to `str`. Only
Tasks' payload carries `owner`/`numberOfMembers`, which is why it's merged
first.

### There is no single "my groups" endpoint

Each service exposes its own. All three are queried and merged rather than
trusting one to be complete. They need no OAuth scope beyond what reading those
services already requires, and return `200` with an empty collection — not an
error — when there are no groups.

---

## Contacts

Zoho Contacts is a genuinely separate product: own base URL
(`contacts.zoho.com`), own scope family (`zohocontacts.contactapi.*`), sharing
only the OAuth token.

### `contact_id` is not globally unique

One account has two contact pools, Personal (`self`) and Organization (`org`).
Fetching an Organization contact's id through the Personal endpoint returns
**HTTP 200** with a *different, partial* record — not a 404. A "try one scope,
fall back on 404" design would silently return the wrong contact.

Every normalized contact therefore carries the `scope` it came from, and
`get_contact` requires `scope` as an argument rather than guessing.

### Archived/inactive filtering is undocumented

Zoho's published parameter list covers `include`, `page`, `per_page`, `sort`,
`filter_categories`, `filter_updated_time`, `fields`, `q` — nothing for
status. Every plausible guess (`status=archived`, `filter_status=archived`,
`include=show_deleted`, `category=archived`) either 404'd or returned zero
results.

The real mechanism is `filter_type=archived` / `filter_type=inactive`, found by
opening the Contacts web app in a logged-in browser, clicking its own
"Archived" folder, and reading the request the page itself made.

### Search matches phone numbers too

`q` matches name, email, *and* phone number server-side.

---

## Resource Booking

Calendar's office-facility feature (`/api/v1/branches` + `/resources`), with
resources organized under a Branch → Building → Floor hierarchy. An empty
`list_branches` is normal — most personal accounts never configure it.

Each resource has an `email`; inviting that address as an event attendee is how
you book it.

### Percent-encoded names were a data-entry artifact, not an API quirk

Worth recording as a correction. The first live test showed every name
percent-encoded (`"Portland%20Branch"`), which looked exactly like the HTML
entity issue in Mail, and `unquote()` was added across all four normalizers.

It turned out to be how the test records had been entered through Zoho's own
creation wizard — a UI bug on the input side, not the read API. Re-fetching
after fixing the names at the source showed clean text. The `unquote()` calls
were **removed** rather than kept "just in case": blind unquoting isn't a safe
no-op (a name containing a literal `%` followed by two hex-like characters
would be corrupted), so defensive code resting on a disproven premise is worse
than none.
