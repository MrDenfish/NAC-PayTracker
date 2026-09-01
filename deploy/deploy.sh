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
# =============================================================================
set -euo pipefail

REPO=${REPO:-/opt/nac-pay}
OWNER=${OWNER:-ubuntu}
BRANCH=${BRANCH:-main}
PUBLIC_URL=${PUBLIC_URL:-https://pch-ledger.com/api/health}
COMPOSE="docker compose -f docker-compose.prod.yml --env-file .env.prod"

die() { echo "DEPLOY FAILED: $*" >&2; exit 1; }
step() { echo; echo "── $* ──"; }

# Always drive git as the repo owner. Running git as root against an
# ubuntu-owned tree is the exact failure this script exists to prevent.
as_owner() { sudo -u "$OWNER" -H git -C "$REPO" "$@"; }

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

step "Building and starting"
cd "$REPO/deploy"
# The SHA is baked in as a build arg so the RUNNING container can be asked
# what it is, rather than inferred from the host's working tree.
NAC_PAY_GIT_SHA="$ACTUAL" $COMPOSE up -d --build

step "Proving the running container is the intended commit"
for i in $(seq 1 30); do
  RUNNING=$(docker exec nac-pay printenv NAC_PAY_GIT_SHA 2>/dev/null || true)
  [ -n "$RUNNING" ] && break
  sleep 2
done
[ -n "${RUNNING:-}" ] || die "container never reported NAC_PAY_GIT_SHA (old image without the build arg?)"
[ "$RUNNING" = "$ACTUAL" ] ||
  die "container is running ${RUNNING:0:7} but we deployed ${ACTUAL:0:7} — STALE IMAGE"
echo "  container reports ${RUNNING:0:7} ✓"

step "Health — container, then the public path"
docker exec nac-pay curl -fsS http://localhost:8000/api/health >/dev/null ||
  die "container health check failed"
echo "  container health ✓"
# Box-local health says nothing about Caddy, Cloudflare, TLS, or DNS. A deploy
# that only checks localhost can leave the public site broken and call it a win.
curl -fsS --max-time 20 "$PUBLIC_URL" >/dev/null ||
  die "public health check failed at $PUBLIC_URL (container is up — suspect Caddy/Cloudflare)"
echo "  public health ✓"

echo
echo "DEPLOY OK — ${BEFORE:0:7} → ${ACTUAL:0:7}"
