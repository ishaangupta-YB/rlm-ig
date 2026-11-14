# syntax=docker/dockerfile:1
#
# The rlm-ig command in a container: ghcr.io/ishaangupta-yb/rlm-ig
#
#   docker build -t rlm-ig .                         the default image (API backends)
#   docker build -t rlm-ig:claude --target claude .  adds the Claude Code CLI
#
# Base images are pinned by digest; Dependabot keeps the digests current.

FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 AS python

# ---------------------------------------------------------------- the wheel
FROM python AS builder
RUN pip install --no-cache-dir --disable-pip-version-check build==1.6.1
WORKDIR /src
# Only what the wheel needs. Hatchling reads README.md for the package metadata.
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN python -m build --wheel --outdir /dist

# ------------------------------------------------------------ the app image
FROM python AS runtime

ARG VERSION=dev
ARG REVISION=unknown
LABEL org.opencontainers.image.title="rlm-ig" \
      org.opencontainers.image.description="Recursive language models over a real Python REPL" \
      org.opencontainers.image.source="https://github.com/ishaangupta-yb/rlm-ig" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}"

# tini is PID 1: it forwards signals to rlm and reaps the worker processes rlm starts.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tini \
 && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 10001 rlm \
 && useradd --uid 10001 --gid 10001 --home-dir /home/rlm --create-home --shell /usr/sbin/nologin rlm

# The wheel is bind-mounted from the builder, so neither it nor the source tree ends up in a layer.
RUN --mount=type=bind,from=builder,source=/dist,target=/tmp/dist \
    set -- /tmp/dist/rlm_ig-*.whl \
 && pip install --no-cache-dir --disable-pip-version-check "$1[anthropic]"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    XDG_STATE_HOME=/home/rlm/.local/state \
    RLM_SESSION_DIR=/home/rlm/.local/state/rlm-ig/sessions

# Traces and sessions live under XDG_STATE_HOME. The directories are writable
# by any uid (sticky, like /tmp), so `docker run --user "$(id -u):$(id -g)"`
# works too, and a new named volume mounted there inherits these modes.
RUN mkdir -p /home/rlm/.local/state/rlm-ig/traces /home/rlm/.local/state/rlm-ig/sessions /work \
 && chown -R 10001:10001 /home/rlm /work \
 && chmod 0755 /home/rlm /home/rlm/.local \
 && chmod 1777 /home/rlm/.local/state /home/rlm/.local/state/rlm-ig \
               /home/rlm/.local/state/rlm-ig/traces /home/rlm/.local/state/rlm-ig/sessions

USER 10001:10001
WORKDIR /work
ENTRYPOINT ["tini", "--", "rlm"]
CMD ["--help"]

# ------------------------------------------------- Claude Code, for :claude
# Installed with npm at a pinned version. npm checks the package integrity
# against the registry. The package ships a native binary, which is all the
# final image keeps: Claude Code does not need Node.js at run time.
FROM node:22-slim@sha256:e9f565d32859f0cbfb52125eafe8be39390f24dac302dc3cdf566e8ba3ab21ee AS claude-code
ARG CLAUDE_CODE_VERSION=2.1.315
RUN npm install --global --no-fund --no-audit "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
 && cp "$(npm root --global)/@anthropic-ai/claude-code/bin/claude.exe" /claude \
 && test -s /claude

FROM runtime AS claude
COPY --from=claude-code --chmod=755 /claude /usr/local/bin/claude
# Updates come with a new image, not from inside a running container.
ENV DISABLE_AUTOUPDATER=1

# ------------------------------------------------------------ the default
# The last stage is what a plain `docker build .` produces.
FROM runtime AS app
