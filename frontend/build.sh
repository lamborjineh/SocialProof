#!/bin/bash
set -e

# Vercel runs this from the root of the repo, with rootDirectory set to /frontend
# So __dirname here IS the frontend/ folder.
# env-config.js must be written here so Vercel serves it at /env-config.js

BACKEND_URL="${BACKEND_URL}"

if [ -z "$BACKEND_URL" ]; then
  echo "ERROR: BACKEND_URL environment variable is not set in Vercel!"
  echo "Go to Vercel → Project Settings → Environment Variables and add BACKEND_URL"
  echo "Falling back to empty string — API calls will 404."
  echo "window.__BACKEND_URL__ = '';" > env-config.js
else
  echo "✅ BACKEND_URL set to: $BACKEND_URL"
  echo "window.__BACKEND_URL__ = '${BACKEND_URL}';" > env-config.js
fi

echo "Generated env-config.js:"
cat env-config.js
