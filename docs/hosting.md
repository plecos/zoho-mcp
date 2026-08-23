# Hosting `zoho-mcp` for a phone (Cloud Run + OAuth)

The Claude mobile apps can only reach a **remote** MCP server, and the "Add
custom connector" dialog has no field for a static token — only a URL and
optional OAuth client fields. So putting Zoho on your phone means hosting this
server publicly and running it in **OAuth mode**, where it acts as its own
OAuth 2.1 authorization server (no third-party login; you approve each
connection with a passphrase you set).

This is the exact sequence used to bring the reference deployment up and verify
it end to end against the real Claude connector — including the two things that
tripped it up the first time (see [Notes](#notes-and-gotchas)). It targets
Google Cloud Run, but nothing here is Cloud-Run-specific beyond the `gcloud`
commands: any container host with a public HTTPS URL and a bit of durable disk
works the same way.

> Cloud Run lives in the same GCP project as Firebase, so a "Firebase project"
> is a fine home for it. Firebase's own serverless products (Cloud Functions)
> are **not** — this is a long-running, stateful server, not a request handler.

## What you'll need

- A GCP project with **billing enabled** (Cloud Run's compute needs it).
- The `gcloud` CLI, authenticated: `gcloud auth login && gcloud config set project YOUR_PROJECT_ID`.
- Your Zoho `client_id` and `client_secret`, and a **refresh token** (below).
- A **Claude plan that allows custom connectors** (Free allows one; Pro/Max more).

Set these shell variables once; the commands below use them:

```bash
PROJECT=YOUR_PROJECT_ID
REGION=us-central1
SERVICE=zoho-mcp
```

## 1. Get a Zoho refresh token

OAuth mode reads the Zoho refresh token from the environment (`ZOHO_TOKEN_STORE=env`),
because a hosted box has no OS keychain. Mint one on a machine that has a
browser, then move it to the host:

```bash
uv run zoho-mcp-setup                 # approve in the browser
# read it back out of that machine's credential store:
uv run python -c "import keyring; print(keyring.get_password('zoho-mcp','zoho_refresh_token'))"
# (bare `keyring` isn't on PATH; on macOS you can also use:
#  security find-generic-password -s zoho-mcp -a zoho_refresh_token -w )
```

It looks like `1000.xxxx…`, doesn't expire on its own, and should be treated
like a password in transit.

## 2. Enable the APIs

```bash
gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com secretmanager.googleapis.com
```

If this errors with `UREQ_PROJECT_BILLING_NOT_FOUND`, billing isn't linked to
*this* project. Check with `gcloud billing projects describe $PROJECT` and link
an open account with `gcloud billing projects link $PROJECT --billing-account=XXXXXX-XXXXXX-XXXXXX`.

## 3. Put the secrets in Secret Manager (recommended)

Inline `--set-env-vars` works for a throwaway test, but the values are then
visible in the service config **and** the shell mangles anything with a comma,
`$`, or quotes — a mangled passphrase silently fails the consent page. Secret
Manager stores bytes verbatim and keeps them out of the config:

```bash
printf '%s' 'YOUR_ZOHO_CLIENT_SECRET'        | gcloud secrets create zoho-client-secret   --data-file=-
printf '%s' '1000.your-refresh-token'         | gcloud secrets create zoho-refresh-token    --data-file=-
printf '%s' 'a-passphrase-you-choose'         | gcloud secrets create zoho-oauth-password    --data-file=-
```

(`printf` rather than `echo` so no trailing newline sneaks into the value.)

## 4. Work out the public URL up front

Cloud Run URLs are deterministic, so you can set the issuer before the first
deploy:

```bash
PROJNUM=$(gcloud projects describe $PROJECT --format='value(projectNumber)')
ISSUER="https://${SERVICE}-${PROJNUM}.${REGION}.run.app"
echo "$ISSUER"
```

The issuer **must** be the real public HTTPS URL — it's what the server puts in
its discovery metadata and tokens, and Claude's cloud connects *to* it, so it
has to be reachable from the internet. (If Cloud Run ever hands you a different
URL, the deploy output shows it; re-run the update in step 6 with the real one.)

## 5. Deploy

From the repo root (the `Dockerfile` is here), build and deploy in OAuth mode.
`--source .` builds the image with Cloud Build — no local Docker needed.

```bash
gcloud run deploy $SERVICE --source . --region $REGION \
  --allow-unauthenticated \
  --set-env-vars "ZOHO_HTTP_AUTH_MODE=oauth,ZOHO_TOKEN_STORE=env,ZOHO_OAUTH_ISSUER=$ISSUER,ZOHO_OAUTH_STATE_DIR=/oauth-state,ZOHO_CLIENT_ID=YOUR_ZOHO_CLIENT_ID" \
  --set-secrets "ZOHO_CLIENT_SECRET=zoho-client-secret:latest,ZOHO_REFRESH_TOKEN=zoho-refresh-token:latest,ZOHO_OAUTH_OPERATOR_PASSWORD=zoho-oauth-password:latest"
```

Two flags that matter and why:

- **`--allow-unauthenticated`** is required and correct: Claude's cloud has no
  GCP credentials, so *this server's* OAuth is the gate, not Cloud Run's IAM.
  The endpoint is not open — every `/mcp` request needs a valid access token.
- The image binds `0.0.0.0:$PORT` (the `Dockerfile` handles it); Cloud Run
  terminates TLS in front.

`ZOHO_OAUTH_STATE_DIR=/oauth-state` points at the durable mount added next.

## 6. Durable state (so scale-to-zero doesn't force a re-consent)

Cloud Run's local disk is ephemeral. If the instance scales to zero and cold
starts, the signing key would regenerate, every issued token would stop
verifying, and the connector would have to re-authorize. Back the state dir
with a private GCS bucket so the key, registered clients, and refresh ledger
survive:

```bash
BUCKET="${PROJECT}-oauth-state"          # must be globally unique
gcloud storage buckets create gs://$BUCKET --location=$REGION --uniform-bucket-level-access

# grant only the Cloud Run runtime service account
SA=$(gcloud run services describe $SERVICE --region $REGION \
  --format='value(spec.template.spec.serviceAccountName)')
SA=${SA:-${PROJNUM}-compute@developer.gserviceaccount.com}
gcloud storage buckets add-iam-policy-binding gs://$BUCKET \
  --member="serviceAccount:$SA" --role="roles/storage.objectAdmin"

# mount it at the state dir
gcloud run services update $SERVICE --region $REGION \
  --execution-environment=gen2 \
  --add-volume=name=oauth-state,type=cloud-storage,bucket=$BUCKET \
  --add-volume-mount=volume=oauth-state,mount-path=/oauth-state
```

That bucket now holds the signing key and any client secrets — keep it private
(the commands above grant only the runtime service account; never add
`allUsers`).

## 7. Cost

The default deploy scales to zero, which is what you want for personal use:

```bash
gcloud run services update $SERVICE --region $REGION --min-instances 0 --cpu-throttling
```

- `--min-instances 0` — no always-on instance; you pay per request. (Set it to
  `1` only if you want to eliminate cold-start latency and are willing to pay
  for a warm instance around the clock.)
- `--cpu-throttling` — CPU billed only during requests.

With durable state (step 6) in place, scale-to-zero costs you nothing but a few
seconds of cold-start latency on the first call after idle — **no** re-consent.

## 8. Add the connector

1. Verify the server is up and speaking OAuth:
   ```bash
   curl -s "$ISSUER/.well-known/oauth-authorization-server"   # metadata JSON
   curl -s -o /dev/null -w '%{http_code}\n' "$ISSUER/mcp"     # 401
   ```
2. On **claude.ai in a browser**: Settings → Connectors → **Add custom connector**.
3. URL: `$ISSUER/mcp`. Leave the OAuth client fields **blank** (Claude
   self-registers via DCR).
4. Claude opens a consent page → enter your operator passphrase → **Approve**.
5. The tools appear, and the connector syncs to your phone app.

Confirm the durable state took:

```bash
gcloud storage ls gs://$BUCKET
# expect: signing_key.json  clients.json  refresh_tokens.json
```

Then the real proof: leave it idle ~20 minutes (so it scales to zero), use it
again — it should work with no consent prompt.

## Updating

Redeploy after pulling new code:

```bash
git pull
gcloud run deploy $SERVICE --source . --region $REGION
```

Env and secrets set previously carry across revisions. Rotating a secret is a
new version plus a no-op deploy:

```bash
printf '%s' 'new-value' | gcloud secrets versions add zoho-oauth-password --data-file=-
gcloud run services update $SERVICE --region $REGION \
  --update-secrets ZOHO_OAUTH_OPERATOR_PASSWORD=zoho-oauth-password:latest
```

## Notes and gotchas

Both of the following are already handled in the code/image; they're recorded
because they were invisible until the server ran on a real host, and they
explain why the `Dockerfile` and server look the way they do.

- **`uv` must be pinned in the image.** The floating `uv:python3.12-bookworm-slim`
  tag resolved to a `uv` older than this project's `required-version`, and the
  build failed at `uv sync`. The `Dockerfile` copies a pinned `uv` binary
  instead.
- **DNS-rebinding protection.** FastMCP's streamable transport allow-lists only
  `localhost` for its host check, so a server on a real hostname returns
  `421 Misdirected Request` ("Invalid Host header"). The hosted transports
  disable that check — it defends a *localhost* server against a browser, and
  is redundant for a remote, authenticated endpoint whose platform only routes
  its own hostname to it. (Content-type validation stays on.)
- **A mangled passphrase fails silently.** If you skip Secret Manager and set
  `ZOHO_OAUTH_OPERATOR_PASSWORD` with `--set-env-vars`, a comma splits it and
  the shell may eat `$`/`!`/quotes — the consent page then rejects what you
  type with a 403 and no other clue. Use Secret Manager (step 3), or a plain
  alphanumeric value.
- **Build/deploy staging accumulates a few MB** in a `run-sources-…` GCS bucket
  (one source snapshot per `--source` deploy). It's fractions of a cent; set a
  lifecycle rule to auto-expire it if you like.
