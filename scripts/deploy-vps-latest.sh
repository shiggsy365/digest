#!/usr/bin/env bash
set -euo pipefail

REPO="${REPO:-shiggsy365/digest}"
HOST="${HOST:-ubuntu@141.147.112.230}"
REMOTE_DIR="${REMOTE_DIR:-/opt/docker}"
ENV_FILE="${ENV_FILE:-apps/digest/.env}"
VERSION="${VERSION:-}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:7654/healthz}"
SERVICES=(${SERVICES:-digest-migrate digest-web digest-worker})

usage() {
  cat <<EOF
Usage: $(basename "$0") [options]

Pull the latest Digest release on the VPS, update the compose version pin, and
recreate the Digest services.

Options:
  --version VERSION     Deploy a specific version or tag, e.g. 1.0.25 or v1.0.25
  --host HOST           SSH host, default: ${HOST}
  --remote-dir DIR      Compose project directory, default: ${REMOTE_DIR}
  --env-file PATH       Env file relative to remote dir, default: ${ENV_FILE}
  --health-url URL      Remote health check URL, default: ${HEALTH_URL}
  -h, --help            Show this help

Environment overrides:
  REPO, HOST, REMOTE_DIR, ENV_FILE, VERSION, HEALTH_URL, SERVICES
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --version)
      VERSION="${2:?Missing value for --version}"
      shift 2
      ;;
    --host)
      HOST="${2:?Missing value for --host}"
      shift 2
      ;;
    --remote-dir)
      REMOTE_DIR="${2:?Missing value for --remote-dir}"
      shift 2
      ;;
    --env-file)
      ENV_FILE="${2:?Missing value for --env-file}"
      shift 2
      ;;
    --health-url)
      HEALTH_URL="${2:?Missing value for --health-url}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

need() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

latest_release_tag() {
  if command -v gh >/dev/null 2>&1; then
    gh release view --repo "$REPO" --json tagName --jq .tagName
    return
  fi

  need curl
  need python3
  python3 - "$REPO" <<'PY'
import json
import sys
import urllib.request

repo = sys.argv[1]
url = f"https://api.github.com/repos/{repo}/releases/latest"
with urllib.request.urlopen(url, timeout=15) as response:
    print(json.load(response)["tag_name"])
PY
}

normalize_version() {
  local tag="$1"
  printf '%s\n' "${tag#v}"
}

need ssh

if [[ -z "$VERSION" ]]; then
  VERSION="$(normalize_version "$(latest_release_tag)")"
else
  VERSION="$(normalize_version "$VERSION")"
fi

if [[ -z "$VERSION" ]]; then
  echo "Could not determine a Digest version to deploy." >&2
  exit 1
fi

echo "Deploying ${REPO}:${VERSION} to ${HOST}:${REMOTE_DIR}"
echo

remote_script=$(cat <<'REMOTE'
set -euo pipefail

read -r -a SERVICE_ARGS <<< "$SERVICES"
cd "$REMOTE_DIR"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Env file not found: $REMOTE_DIR/$ENV_FILE" >&2
  exit 1
fi

backup="${ENV_FILE}.bak.$(date +%Y%m%d%H%M%S)"
cp "$ENV_FILE" "$backup"

if grep -q '^DIGEST_VERSION=' "$ENV_FILE"; then
  sed -i "s/^DIGEST_VERSION=.*/DIGEST_VERSION=${VERSION}/" "$ENV_FILE"
else
  printf '\nDIGEST_VERSION=%s\n' "$VERSION" >> "$ENV_FILE"
fi

echo "Updated $ENV_FILE:"
grep -n '^DIGEST_VERSION=' "$ENV_FILE"
echo "Backup: $backup"
echo

echo "Resolved Digest images:"
docker compose config --images | grep 'ghcr.io/shiggsy365/digest' || true
echo

docker compose pull "${SERVICE_ARGS[@]}"
docker compose up -d "${SERVICE_ARGS[@]}"
echo

docker compose images "${SERVICE_ARGS[@]}"
echo

docker compose ps "${SERVICE_ARGS[@]}"
echo

if [[ -n "$HEALTH_URL" ]]; then
  echo "Health check: $HEALTH_URL"
  curl -fsS "$HEALTH_URL"
  echo
fi
REMOTE
)

printf -v remote_env 'REMOTE_DIR=%q ENV_FILE=%q VERSION=%q HEALTH_URL=%q SERVICES=%q' \
  "$REMOTE_DIR" "$ENV_FILE" "$VERSION" "$HEALTH_URL" "${SERVICES[*]}"

ssh "$HOST" \
  "$remote_env bash -s" \
  <<< "$remote_script"

echo
echo "Deploy complete: ${REPO}:${VERSION}"
