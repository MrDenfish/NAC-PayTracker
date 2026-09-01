#!/usr/bin/env bash
# =============================================================================
# NAC-Pay production deploy — pull, rebuild, and PROVE what is running.
#
#   sudo /opt/nac-pay/deploy/deploy.sh
#
# Why this exists: on 2026-08-31 a deploy reported success and a healthy
# container while serving code from three PRs earlier. `git pull` had failed
# ("detected dubious ownership": the repo is owned by `ubuntu`, and both SSM
# and sudo run as root), the build reused the old source, and the health check
# passed because the OLD app was perfectly healthy. Liveness never proves
# identity. Every step below is asserted, and the script exits non-zero the
# moment reality diverges from intent.
#
# Two stages, deliberately. This script lives IN the repo it pulls, and bash
# reads a script lazily by byte offset — so a pull that rewrites deploy.sh
# mid-run makes bash execute a mixture of old and new code. (That is not
# hypothetical: it silently ran the pre-fix health check on 2026-08-31.)
# Stage 1 pulls, then re-execs a /tmp COPY of the freshly pulled script as
# stage 2, which is therefore immune to further edits.
# =============================================================================
set -euo pipefail

REPO=${REPO:-/opt/nac-pay}
OWNER=${OWNER:-ubuntu}
BRANCH=${BRANCH:-main}
SITE_HOST=${SITE_HOST:-pch-ledger.com}
HEALTH_PATH=${HEALTH_PATH:-/api/health}
COMPOSE="docker compose -f docker-compose.prod.yml --env-file .env.prod"

die() { echo "DEPLOY FAILED: $*" >&2; exit 1; }
step() { echo; echo "── $* ──"; }
as_owner() { sudo -u "$OWNER" -H git -C "$REPO" "$@"; }

# ── Stage 1: pull, then hand off to a pinned copy of the new script ─────────
if [ "${DEPLOY_STAGE:-1}" = "1" ]; then
  [ -d "$REPO/.git" ] || die "$REPO is not a git repository"

  step "Resolving intent"
  EXPECTED=$(as_owner ls-remote origin "$BRANCH" | cut -f1)
  [ -n "$EXPECTED" ] || die "could not resolve origin/$BRANCH"
  BEFORE=$(as_owner rev-parse HEAD)
  echo "  current: ${BEFORE:0:7}"
  echo "  target : ${EXPECTED:0:7}"

  step "Pulling"
  as_owner pull --ff-only origin "$BRANCH"
  ACTUAL=$(as_owner rev-parse HEAD)
  [ "$ACTUAL" = "$EXPECTED" ] ||
    die "HEAD is ${ACTUAL:0:7} but origin/$BRANCH is ${EXPECTED:0:7} — pull did not take"
  echo "  now at : ${ACTUAL:0:7}"

  PINNED=$(mktemp /tmp/nac-pay-deploy.XXXXXX.sh)
  cp "$REPO/deploy/deploy.sh" "$PINNED"
  chmod +x "$PINNED"
  trap 'rm -f "$PINNED"' EXIT
  DEPLOY_STAGE=2 DEPLOY_SHA="$ACTUAL" DEPLOY_PREV="$BEFORE" "$PINNED"
  exit $?
fi

# ── Stage 2: build and verify (running from /tmp, cannot be rewritten) ──────
ACTUAL=${DEPLOY_SHA:?stage 2 requires DEPLOY_SHA}
BEFORE=${DEPLOY_PREV:-unknown}

step "Building and starting"
cd "$REPO/deploy"
# The SHA is baked in as a build arg so the RUNNING container can be asked
# what it is, rather than inferred from the host's working tree.
NAC_PAY_GIT_SHA="$ACTUAL" $COMPOSE up -d --build

step "Proving the running container is the intended commit"
RUNNING=""
for _ in $(seq 1 30); do
  RUNNING=$(docker exec nac-pay printenv NAC_PAY_GIT_SHA 2>/dev/null || true)
  [ -n "$RUNNING" ] && break
  sleep 2
done
[ -n "$RUNNING" ] || die "container never reported NAC_PAY_GIT_SHA (old image without the build arg?)"
[ "$RUNNING" = "$ACTUAL" ] ||
  die "container is running ${RUNNING:0:7} but we deployed ${ACTUAL:0:7} — STALE IMAGE"
echo "  container reports ${RUNNING:0:7} ✓"

step "Health — container, then the public path"
# A freshly recreated container answers `printenv` immediately but is not
# serving yet: uvicorn needs a moment to bind, and the compose healthcheck
# allows a 20s start period. Probing once, instantly, fails a deploy that is
# actually fine — so wait for readiness rather than asserting it.
wait_for() {
  local what=$1 tries=$2 delay=$3; shift 3
  for _ in $(seq 1 "$tries"); do
    if "$@" >/dev/null 2>&1; then echo "  $what ✓"; return 0; fi
    sleep "$delay"
  done
  return 1
}

wait_for "container health" 30 2 \
  docker exec nac-pay curl -fsS http://localhost:8000/api/health ||
  die "container never became healthy (60s)"

# A container health check says nothing about Caddy, TLS, or routing, so also
# go in through the front door — but at 127.0.0.1, NOT through Cloudflare.
# Fetching the real public URL from the box returns 403: Cloudflare's WAF
# (added during the 2026-08 signup-abuse hardening) blocks the origin's own
# egress. --resolve keeps the SNI and Host correct while pinning the
# connection to local Caddy; -k because the origin cert is Cloudflare Origin
# CA, which is deliberately not publicly trusted.
wait_for "edge health (via Caddy)" 10 3 \
  curl -fsSk --resolve "${SITE_HOST}:443:127.0.0.1" --max-time 20 \
    "https://${SITE_HOST}${HEALTH_PATH}" ||
  die "Caddy did not serve ${SITE_HOST}${HEALTH_PATH} (container is up — suspect Caddy config or the amis-internal network)"

echo
echo "DEPLOY OK — ${BEFORE:0:7} → ${ACTUAL:0:7}"
echo
# Everything above runs ON the origin, so it cannot prove Cloudflare, DNS, or
# the public TLS path. Confirm from off-box after deploying:
echo "  Still to confirm from OFF the box:  curl -fsS https://${SITE_HOST}${HEALTH_PATH}"
