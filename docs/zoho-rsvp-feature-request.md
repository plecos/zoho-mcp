# Zoho Calendar API — Feature Request: OAuth invitee RSVP

**Subject:** Feature request: OAuth API endpoint to set the authenticated user's
own RSVP (attendance status) on a received event invitation

---

**Product:** Zoho Calendar — Developer / REST API (OAuth)
**Scope held during testing:** `ZohoCalendar.event.ALL`

## Summary

There is no way, through the public OAuth Calendar API, for the authenticated
user to set their OWN RSVP / attendance response (ACCEPTED / DECLINED /
TENTATIVE) on an event they were INVITED to. This is possible in the Zoho web
and mobile clients, but not via the public API. Please expose an OAuth-writable
path for an invitee's own response.

## What works today (organizer only)

```
PATCH https://calendar.zoho.com/api/v1/calendars/{cal}/events/{event}
      ?attendeedata={"attendees":[{"email":"me@example.com","status":"ACCEPTED"}],
        "notify_attendee":0}
```

- Returns `200` when the caller's role on the event is `organizer`.
- Returns `403 OPERATION_NOT_PERMITTED` when the caller is an `attendee`.
- The docs note `attendeedata` requires "add participants" permission
  (permission level `2` / Invite). An ordinary invitee holds level `1` (View),
  which the organizer sets — so an invitee can never meet this bar.
- `PUT .../events/{event}?eventdata={...attendees...}` (full update) also
  returns `403` for an attendee, for the same reason.

## The invitee's response is modeled but read-only

`GET .../events/{event}` returns a top-level field `rsvpStatus` that reflects the
CALLER's own response:

    0 = NEEDS-ACTION, 1 = ACCEPTED, 2 = DECLINED, 3 = TENTATIVE

This field is read-only — sending it as a query param, or inside `eventdata` /
`attendeedata`, is rejected (`EXTRA_PARAM_FOUND` / `EXTRA_KEY_FOUND_IN_JSON`).

## The web client uses a non-OAuth endpoint

The web UI performs an invitee RSVP via:

```
PUT https://calendar.zoho.com/zcal/calendars/{cal}/events/{event}
    (multipart/form-data field "statusdata")
```

This is authenticated by session cookie + CSRF, NOT OAuth. Sending a valid
`Authorization: Zoho-oauthtoken <token>` bearer to this endpoint returns
`400 INVALID_CSRF_TOKEN` — it does not accept OAuth credentials.

## The request

Please provide an OAuth-accessible way for the authenticated user to set their
OWN attendance status on an event where they are an attendee — for example:

- **(a)** a writable `rsvpStatus` on the event `PATCH`, gated only on "the caller
  is an attendee of this event" (not on organizer/Invite privilege), or
- **(b)** a dedicated endpoint such as
  `PATCH/POST .../api/v1/calendars/{cal}/events/{event}/rsvp`
  with body `{"status":"ACCEPTED|DECLINED|TENTATIVE"}`.

Ideally with optional per-occurrence targeting for recurring events (a
`recurrenceid` in ICS `yyyyMMddTHHmmssZ` form, plus `recurrence_edittype`).

## Reproduction

1. Have user B (organizer) invite user A to an event; A gets View (permission 1).
2. As user A, obtain an OAuth token with `ZohoCalendar.event.ALL`.
3. `PATCH .../events/{event}?attendeedata={"attendees":[{"email":"A","status":"ACCEPTED"}],"notify_attendee":0}` → `403 OPERATION_NOT_PERMITTED`.
4. `GET .../events/{event}` shows `"rsvpStatus":0` for A, but it cannot be written.

## Impact

Any OAuth integration (including MCP servers and unified-API providers) can read
a user's calendar and see pending invitations, but cannot let the user respond
to them — the single most common calendar action. A read-only `rsvpStatus` plus
an organizer-gated writer leaves invitee RSVP unreachable via the API.
