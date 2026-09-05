#!/bin/sh
# Container entrypoint for analyze_server.py.
#
# API keys reach the server as plain environment variables (load_env overlays
# the process environment on top of .env, and the image ships no .env).
# Firestore is the exception: firebase-admin authenticates through Application
# Default Credentials, which insists on a *file* path in
# GOOGLE_APPLICATION_CREDENTIALS. Fly stores the service account as one secret,
# so materialise it here rather than baking the private key into the image.
set -eu

if [ -n "${FIREBASE_SERVICE_ACCOUNT_JSON:-}" ]; then
  credentials=/tmp/firebase-service-account.json
  # umask first: the file must never exist, even briefly, as world-readable.
  ( umask 077; printf '%s' "$FIREBASE_SERVICE_ACCOUNT_JSON" > "$credentials" )
  GOOGLE_APPLICATION_CREDENTIALS="$credentials"
  export GOOGLE_APPLICATION_CREDENTIALS
else
  # Not fatal: Store falls back to an in-memory backend and the page footnote
  # reports it, so the deploy still comes up serving every page.
  echo "WARNING: FIREBASE_SERVICE_ACCOUNT_JSON unset; analyses will not persist" >&2
fi

# ANALYZE_ARGS is deliberately unquoted: it carries zero or more flags.
# shellcheck disable=SC2086
exec python3 analyze_server.py \
  --bind 0.0.0.0 \
  --port "${PORT:-8080}" \
  ${ANALYZE_ARGS:-}
