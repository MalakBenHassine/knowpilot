#!/usr/bin/env bash
# Fills every empty PASSWORD and SECRET of an env file, in place.
#
#   ./infra/server/fill-secrets.sh                  # .env.production
#   ./infra/server/fill-secrets.sh .env.staging
#
# Run once, on the server, right after copying the example (docs/deploy.md).
# It exists because filling a dozen blank lines by hand is one forgotten line
# and a stack that refuses to start - or, worse, a password chosen by a human
# in a hurry.
#
# What it does NOT touch:
#   - values that are already set, so running it twice changes nothing and
#     never invalidates a password Keycloak or PostgreSQL already stores;
#   - anything that is not a password or a secret: the domain, the URLs and
#     the Groq key are decisions and credentials, not random bytes.
#
# Nothing is ever printed. The generated values exist only in the file, which
# this script keeps at mode 600.
set -euo pipefail

FILE="${1:-.env.production}"

if [[ ! -f "$FILE" ]]; then
    echo "No $FILE. Copy it first:" >&2
    echo "    cp .env.production.example $FILE" >&2
    exit 1
fi

command -v openssl >/dev/null || { echo "openssl is required" >&2; exit 1; }

# Only the file's owner, before anything is written into it.
chmod 600 "$FILE"

generated=0
kept=0
output=""

while IFS= read -r line || [[ -n "$line" ]]; do
    if [[ "$line" =~ ^([A-Z0-9_]*(PASSWORD|SECRET))=(.*)$ ]]; then
        name="${BASH_REMATCH[1]}"
        value="${BASH_REMATCH[3]}"
        if [[ -z "$value" ]]; then
            # hex, not base64: the Redis password travels inside a URL, where
            # a `/` or a `+` would have to be escaped by every reader of it.
            line="$name=$(openssl rand -hex 32)"
            generated=$((generated + 1))
        else
            kept=$((kept + 1))
        fi
    fi
    output+="$line"$'\n'
done < "$FILE"

printf '%s' "$output" > "$FILE"
chmod 600 "$FILE"

echo "$FILE: $generated generated, $kept already set."
echo
echo "Still yours to fill in, because they are decisions, not random bytes:"
grep -n '^[A-Z0-9_]*=$' "$FILE" | sed 's/=$//' | sed 's/^/    /' || echo "    (none)"
