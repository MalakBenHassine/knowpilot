# syntax=docker/dockerfile:1.10
#
# A React build is not a program you run - it is a folder of files somebody
# else serves. So the runtime image contains no Node at all: the toolchain
# that produced the bundle has no reason to exist in production, where it
# would only be several hundred megabytes of extra attack surface.
#
#   docker build -t knowpilot-frontend:dev ./frontend
#   docker run --rm -p 8080:8080 knowpilot-frontend:dev


# ============================================================================
# Stage 1 - builder: TypeScript and Vite
# ============================================================================
# Debian slim rather than Alpine: the toolchain ships native binaries (the
# bundler and the linter are Rust programs), and musl builds of those are the
# kind of thing that works until the day it does not. The size of this stage
# is irrelevant - none of it ships.
FROM node:24-slim AS builder

WORKDIR /app

# Manifest and lock file first, for the same reason as the backend: this layer
# is rebuilt only when a dependency changes, not on every edit to a component.
COPY package.json package-lock.json ./

# `npm ci`, never `npm install`.
#
# `ci` installs exactly what package-lock.json says, fails if the lock file
# disagrees with package.json, and deletes node_modules first. `install` is
# allowed to RESOLVE - it can quietly pick a newer version, which means the
# image contains something nobody tested.
#
# `--ignore-scripts` stops every package's install hooks from running.
# Those hooks are the shortest path from a compromised npm release to code
# executing inside the build. Nothing here needs them.
RUN --mount=type=cache,target=/root/.npm \
    npm ci --ignore-scripts

COPY . .

# THE trap of any frontend image, and it is worth reading twice.
#
# A Vite variable is not runtime configuration. It is a COMPILE-TIME constant:
# `import.meta.env.VITE_API_MODE` is literally replaced by its value during
# the build and ends up as text inside a .js file served to every visitor.
#
# Two consequences that catch everybody once.
#
# One: the value must be given HERE, at build time. Passing it with
# `docker run -e` changes nothing at all - the bundle was written hours ago.
# An image built for staging cannot be promoted to production by changing an
# environment variable; it must be rebuilt.
#
# Two: never put a secret in one. Every VITE_ value is public by construction,
# because it is shipped to the browser. The API key of this project is read by
# the backend and never reaches here, which is exactly why the browser talks
# to our own API rather than to the provider.
ARG VITE_API_MODE=http
ENV VITE_API_MODE=${VITE_API_MODE}

# `tsc -b && vite build`: the type check runs FIRST and the build fails on a
# type error. A CI that compiles what does not typecheck is a CI that ships it.
RUN npm run build


# ============================================================================
# Stage 2 - runtime: nginx, and nothing else
# ============================================================================
# The unprivileged variant of the official image: it runs as uid 101 and
# listens on 8080 out of the box. The standard nginx image starts its master
# process as root, which a static file server has no need to be.
FROM nginxinc/nginx-unprivileged:1.29-alpine AS runtime

LABEL org.opencontainers.image.title="knowpilot-frontend" \
      org.opencontainers.image.description="KnowPilot single-page application" \
      org.opencontainers.image.licenses="MIT"

# Replaces the default site. Copied before the files it serves, because the
# configuration changes far less often than the application.
COPY nginx.conf /etc/nginx/conf.d/default.conf

# The only thing that crosses from the build stage: the folder of static
# files. No node_modules, no source, no TypeScript, no bundler.
COPY --from=builder /app/dist /usr/share/nginx/html

# Already the image default, stated so a reader does not have to look it up.
USER nginx

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD wget --quiet --tries=1 --spider http://127.0.0.1:8080/healthz || exit 1

# Inherited from the base image and repeated for the reason every CMD in this
# project is repeated: `daemon off` keeps nginx in the foreground. A web
# server that daemonises exits immediately, and the container dies "healthy".
CMD ["nginx", "-g", "daemon off;"]
