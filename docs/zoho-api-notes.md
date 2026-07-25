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

### Drafts are invisible to the search API

`in:Drafts` returns an empty list even when drafts demonstrably exist. To find
drafts, use `list_emails(folder_id=...)` with the Drafts folder id from
`list_folders`.

### `previousFolderId` is not a parent pointer

Despite the name, it's a display-order "previous sibling" pointer — folders form
a linked list, not a tree, in that field. The real hierarchy signal is the
folder's `path` (e.g. `/Inbox/Work`). `previousFolderId` is excluded from the
normalized shape because it invites exactly the wrong inference.

### Custom folders all report `folderType: "Inbox"`

Every user-created folder, including mail-rule destinations and nested
subfolders, reports type `Inbox`. That's what makes excluding
Sent/Drafts/Templates *by type* safe — it can never accidentally catch a user's
own folder.

### "Notification" is a real folder *and* a label

Zoho files certain mail into a genuine `/Notification` folder (`folderType:
"Inbox"`, like any custom folder) and *also* tags it with a matching label.
Both `in:Notification` and `label:Notification` work.

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
