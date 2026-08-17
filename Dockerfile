# Container image for the hosted (streamable HTTP) transport, for Cloud Run or
# any container host. The stdio server and the MCPB bundle do not use this.
#
# uv is pinned by copying its binary from a versioned image into a standard
# Python base -- Astral's documented pattern. The moving `uv:python3.12-*` tag
# turned out to lag well behind (it resolved to 0.9.30, below this project's
# `required-version = ">=0.11.29"`, and the build failed), so it is not
# trusted here. Bump the pin deliberately rather than tracking a floating tag.
#
# Cloud Run sends traffic to $PORT and requires listening on 0.0.0.0; the CMD
# maps both onto the server's own settings. Everything secret -- auth mode,
# issuer, operator passphrase, Zoho credentials -- is supplied as environment
# at deploy time; nothing is baked into the image.
FROM python:3.12-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /uvx /bin/

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
