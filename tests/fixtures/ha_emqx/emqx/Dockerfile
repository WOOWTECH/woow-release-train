ARG BUILD_FROM=ghcr.io/hassio-addons/debian-base:9.5.0
# hadolint ignore=DL3006
FROM ${BUILD_FROM}

# Set shell
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Setup base system
ARG BUILD_ARCH=amd64
# EMQX stays on the 5.8 line: v5.8.9 is the last Apache-2.0 open-source
# release. 5.9+ and 6.x are the Business Source License enterprise edition,
# which may not be shipped embedded in this add-on. CI rejects anything else.
ARG EMQX_VERSION="v5.8.9"
# SHA-256 of emqx-<version>-debian13-<arch>.tar.gz, copied from the
# .sha256 files published with the upstream GitHub release.
ARG EMQX_SHA256_AMD64="c3b5a8f7c83601867d0fc1ef19a851f6f16f56a9436de74cb05f81f188a44a90"
ARG EMQX_SHA256_ARM64="524b5dd8ccad1f7dc5e5c15171328618e1ea7703fed56e17006a27258a13ecd4"
# ngrok agent, pinned from ngrok's apt repository (the SHA-256 values come
# from its dists/*/main/binary-<arch>/Packages index).
ARG NGROK_VERSION="3.39.11"
ARG NGROK_SHA256_AMD64="e51aa234283bcf20a777e21a624b2a3501b058b3d5a1faec59106311dbe30395"
ARG NGROK_SHA256_ARM64="b307de23bddc13a5729b2c8d2283fe98166e2d211d59ceeb62204ff216798bcc"
ARG NGROK_BASE_URL="https://ngrok-agent.s3.amazonaws.com/pool/main/n/ngrok"
# This image is built on the customer's Home Assistant host, often over a
# flaky link (MTU trouble): downloads retry, abort when they stall so the
