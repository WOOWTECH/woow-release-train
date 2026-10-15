# PaaS-only layer over the exact currently deployed non-root LiteLLM base.
# amd64 manifest: sha256:843a35c72a832e9c062b66111b1aa54e6588fb32c3ab400d3e20ea9c5fa656a0
ARG BASE=jcr-prod.woowtech.io/woow-paas-docker-local/litellm@sha256:37066242e6b8327fdd627682a66a9ae2a599553b4d5a84c905c6d8b9eb35c01e
FROM ${BASE}
ARG SOURCE_REVISION
LABEL org.opencontainers.image.source="https://git-prod.woowtech.io/woow-paas/paas-odoo-ci" \
      org.opencontainers.image.revision="${SOURCE_REVISION}" \
      io.woow.litellm.base.index="sha256:37066242e6b8327fdd627682a66a9ae2a599553b4d5a84c905c6d8b9eb35c01e" \
      io.woow.litellm.base.amd64="sha256:843a35c72a832e9c062b66111b1aa54e6588fb32c3ab400d3e20ea9c5fa656a0"
USER 0
COPY --chown=0:0 image_contract.py run_image_security.py run_image_api.py bundle.json /opt/woow/litellm-image/
COPY --chown=0:0 vendor/ /opt/woow/litellm-image/vendor/
