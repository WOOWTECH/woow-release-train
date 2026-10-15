# Upstream n8n release wrapped by this add-on. The editor and the task runners
# must come from the same release, so both images are derived from this one
# value. The add-on's own version (config.yaml) may advance ahead of it.
ARG N8N_UPSTREAM_VERSION=2.43.6

ARG BUILD_FROM=docker.io/n8nio/n8n:${N8N_UPSTREAM_VERSION}

ARG BUILD_VERSION=2.43.6

FROM docker.io/n8nio/runners:${N8N_UPSTREAM_VERSION} AS runner

# The Python task runner's virtualenv and its compiled dependencies are built
# for the runners image's own CPython (3.13 for n8n 2.43), not for the
# distribution python3 of the n8n base (3.14). Stage that interpreter, found
# through the venv's own symlink, so it is copied without naming a version.
FROM runner AS runner-python
# hadolint ignore=DL3002
USER root
RUN set -eu; \
    py="$(readlink -f /opt/runners/task-runner-python/.venv/bin/python)"; \
    name="$(basename "${py}")"; \
    mkdir -p /woow-python/usr/local/bin /woow-python/usr/local/lib; \
    cp -a "${py}" /woow-python/usr/local/bin/; \
    cp -a "/usr/local/lib/${name}" /woow-python/usr/local/lib/; \
    cp -a /usr/local/lib/lib"${name}".so* /woow-python/usr/local/lib/

FROM ${BUILD_FROM}

# n8n environment variables
#
