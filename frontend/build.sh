#!/bin/bash
# Vercel build script - injects backend URL from env var
BACKEND_URL="${VITE_BACKEND_URL:-$BACKEND_URL}"
if [ -z "$BACKEND_URL" ]; then
  echo "WARNING: BACKEND_URL not set. API calls will be relative (same origin)."
  echo "window.__BACKEND_URL__ = '';" > env-config.js
else
  echo "Setting backend URL to: $BACKEND_URL"
  echo "window.__BACKEND_URL__ = '${BACKEND_URL}';" > env-config.js
fi
