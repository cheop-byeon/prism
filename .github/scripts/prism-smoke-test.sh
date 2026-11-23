#!/usr/bin/env bash
# Smoke test a deployed Prism from the outside: TLS, nginx, the Dify API and
# Prism's sign-in page must all answer. Retries for up to 5 minutes, since
# containers are still warming up right after deploy/rebuild.
#
# Usage: prism-smoke-test.sh https://staging.prism.uiocloud.no
set -euo pipefail

base="${1:?usage: $0 BASE_URL}"
deadline=$((SECONDS + 300))

check() {
  local page
  curl -fsS -m 15 -o /dev/null "$base/console/api/system-features" &&
    # Read the whole page before grep: with pipefail, `curl | grep -q` fails
    # whenever grep exits on its first match before curl finishes writing.
    page="$(curl -fsSL -m 15 "$base/prism/login")" &&
    grep -q "Prism" <<<"$page"
}

until check; do
  if [ "$SECONDS" -ge "$deadline" ]; then
    echo "smoke test FAILED: $base did not answer correctly within 5 minutes" >&2
    exit 1
  fi
  echo "not ready yet, retrying in 15s..."
  sleep 15
done
echo "smoke test passed: $base"
