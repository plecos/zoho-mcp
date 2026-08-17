# Container image for the hosted (streamable HTTP) transport, for Cloud Run or
# any container host. The stdio server and the MCPB bundle do not use this.
#
# Cloud Run sends traffic to $PORT and requires listening on 0.0.0.0; the CMD
# maps both onto the server's own settings. Everything else -- auth mode,
# issuer, operator passphrase, Zoho credentials -- is supplied as environment
# at deploy time; nothing secret is baked into the image.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

# Metadata files the build backend needs, then the lockfile and source. Copied
# before syncing so the dependency layer caches across source-only changes.
COPY pyproject.toml uv.lock README.md LICENSE NOTICE ./
COPY src ./src

RUN uv sync --frozen --no-dev

# Bind every interface: on Cloud Run the platform's ingress is the only path in
# and it terminates TLS, so loopback (the local default) would be unreachable.
ENV ZOHO_HTTP_HOST=0.0.0.0

CMD ["sh", "-c", "ZOHO_HTTP_PORT=${PORT:-8080} uv run --no-dev zoho-mcp-http"]
