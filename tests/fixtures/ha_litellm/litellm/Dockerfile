# Woow LiteLLM add-on image. Prebuilt by CI; never built on Home Assistant.
#
# Owner decision 2026-10-09: a PRIVATE image for WOOWTECH's own Home Assistant hosts (private GHCR
# package, pulled by the Supervisor with registry credentials), built like the WOOW PaaS image:
# directly on ghcr.io/berriai/litellm-non_root:1.104.0 (the very image the PaaS runs), enterprise
# code kept, nothing pruned or flattened. On top: PostgreSQL 18, nginx, curl and jq from public
# Wolfi (the whole dependency closure pinned, SP2), s6-overlay, and the add-on's rootfs.
#
# Every pin here must equal litellm/upstream.lock.json (tests/test_config_contract.py).
# The ARG is not called BUILD_FROM on purpose: the Supervisor overrides BUILD_FROM.
ARG LITELLM_IMAGE=ghcr.io/berriai/litellm-non_root:1.104.0@sha256:37066242e6b8327fdd627682a66a9ae2a599553b4d5a84c905c6d8b9eb35c01e

# s6-overlay v3.2.3.2 tarballs, checked by sha256 (lock: s6_overlay). Only bind-mounted below,
# so the tarballs themselves never become part of a layer.
FROM scratch AS s6-overlay
ADD --checksum=sha256:5379750ed30a84bbd2e2dd74847ba6b5bd29cd0b2e3ea2ec58049b57eb2eda12 \
    https://github.com/just-containers/s6-overlay/releases/download/v3.2.3.2/s6-overlay-noarch.tar.xz /s6-overlay-noarch.tar.xz
ADD --checksum=sha256:e6befcc96a437a3831386ecfc51808c5d3e939dc5fe3c02ae9284599e8aa2408 \
    https://github.com/just-containers/s6-overlay/releases/download/v3.2.3.2/s6-overlay-x86_64.tar.xz /s6-overlay-x86_64.tar.xz

FROM ${LITELLM_IMAGE} AS runtime
# buildx --platform linux/amd64 resolves the index to sha256:843a35c72a832e9c062b66111b1aa54e6588fb32c3ab400d3e20ea9c5fa656a0.
# Upstream ends with USER 65534; s6-overlay's /init must run as root (it drops privileges per service).
USER root
SHELL ["/bin/sh", "-eu", "-c"]
