# syntax=docker/dockerfile:1.10
#
# Keycloak for production: built once, started with --optimized.
#
# `start` without `--optimized` re-runs Keycloak's build step on EVERY boot -
# about thirty seconds of Quarkus augmentation before the first login can be
# served, repeated on each restart. Build-time options (database vendor, the
# /auth path, health endpoints) are baked into the image here instead, the way
# the Keycloak documentation recommends for production.
#
# The development stack keeps `start-dev` on the stock image: it disables the
# HTTPS and hostname checks, which is what a laptop wants and exactly what
# production must not have.

# Same version as docker-compose.yml, pinned by digest (multi-architecture
# index, so the image builds on x86 and ARM alike).
ARG KEYCLOAK_IMAGE=quay.io/keycloak/keycloak:26.7.4@sha256:82a77884f3af238beab1e7afd63b5f530e1b5c0590bd7aa60b40a40463e29b2c

FROM ${KEYCLOAK_IMAGE} AS builder

# Build-time options: they change the compiled server, not its configuration.
#
# KC_HTTP_RELATIVE_PATH=/auth - Keycloak is served under the application's own
# domain, next to the SPA and the API, so ONE certificate and one DNS name
# cover the whole product. The reverse proxy routes /auth here.
ENV KC_DB=postgres \
    KC_HEALTH_ENABLED=true \
    KC_HTTP_RELATIVE_PATH=/auth

RUN /opt/keycloak/bin/kc.sh build


FROM ${KEYCLOAK_IMAGE}

LABEL org.opencontainers.image.title="knowpilot-keycloak" \
      org.opencontainers.image.description="Keycloak, pre-built for KnowPilot production" \
      org.opencontainers.image.licenses="MIT"

COPY --from=builder /opt/keycloak/ /opt/keycloak/

# The stock image already runs as the unprivileged `keycloak` user (uid 1000).
USER 1000

ENTRYPOINT ["/opt/keycloak/bin/kc.sh"]
CMD ["start", "--optimized"]
