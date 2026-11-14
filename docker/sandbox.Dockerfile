# syntax=docker/dockerfile:1
#
# The image the REPL worker runs in under `--sandbox docker`:
# ghcr.io/ishaangupta-yb/rlm-ig-sandbox
#
#   docker build -t rlm-ig-sandbox -f docker/sandbox.Dockerfile docker
#
# It holds Python and a few libraries the model's code often wants. It holds
# no rlm-ig: the worker's source arrives over stdin when the worker starts.

FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b

ARG VERSION=dev
ARG REVISION=unknown
LABEL org.opencontainers.image.title="rlm-ig-sandbox" \
      org.opencontainers.image.description="Locked-down Python for the rlm-ig REPL worker" \
      org.opencontainers.image.source="https://github.com/ishaangupta-yb/rlm-ig" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}"

RUN pip install --no-cache-dir --disable-pip-version-check \
      numpy==2.5.4 \
      pandas==3.1.0 \
      python-dateutil==2.9.0.post0 \
      regex==2026.9.29

# The preset runs the container with a read-only root, so no .pyc files.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

USER 65534:65534
WORKDIR /tmp
CMD ["python3"]
