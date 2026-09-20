#!/usr/bin/env bash
# Box A's deploy hook: rebuild and restart PerfLab from a pushed commit.
#
# This is the "pipeline" in `deploy.mode: pipeline` (DESIGN.md 19.5). There is no
# Jenkins and no GitHub Actions on the target box; a post-receive hook on a bare
# repo is the whole of it, and that is deliberate -- the deploy path should be
# something an operator can read in one sitting.
#
# INSTALL (on Box A, once):
#   git init --bare ~/perflab-deploy.git
#   # the hook lives in Crucible's repo; copy it across
#   cp <crucible>/scripts/box_a_post_receive.sh ~/perflab-deploy.git/hooks/post-receive
#   chmod +x ~/perflab-deploy.git/hooks/post-receive
#   git -C ~/perflab-deploy.git config core.hooksPath hooks
# then on the Crucible box:
#   git remote add perftest <boxa-user>@10.0.0.79:perflab-deploy.git
# and only then flip config/profiles/spring-boot.yaml to `deploy.mode: pipeline`.
#
# Crucible never composes this command and never sees the box's credentials
# (19.8): it pushes over an SSH deploy key referenced by path, and everything
# below runs on Box A under the box's own account.
#
# The one rule this script must not break: the jar is built with the sha baked
# in, so /api/version can prove which commit is running. Crucible polls that and
# refuses to measure until it matches (19.6). Building without -Dperflab.commit
# would leave the field null and stall every campaign -- correctly, but for a
# reason nobody would enjoy debugging.
set -euo pipefail

BRANCH="${PERFLAB_DEPLOY_BRANCH:-perftest_sandbox}"
WORKTREE="${PERFLAB_WORKTREE:-$HOME/perflab-work}"
LOG="${PERFLAB_DEPLOY_LOG:-$HOME/perflab-deploy.log}"
HEALTH_URL="${PERFLAB_HEALTH_URL:-http://127.0.0.1:8080/actuator/health}"
VERSION_URL="${PERFLAB_VERSION_URL:-http://127.0.0.1:8080/api/version}"
# Only the Spring profile. Every TUNABLE comes from application.properties,
# because that file is what the agent edits -- see the launch block below.
SPRING_PROFILE="${PERFLAB_SPRING_PROFILE:-lab}"

log() { printf '%s %s\n' "$(date -Is)" "$*" | tee -a "$LOG"; }

deploy_sha=""
while read -r _old new ref; do
  if [ "$ref" = "refs/heads/$BRANCH" ]; then
    deploy_sha="$new"
  fi
done

if [ -z "$deploy_sha" ]; then
  # A push to some other branch is not an error; it is simply not a deploy.
  exit 0
fi

log "deploy requested: $deploy_sha on $BRANCH"

mkdir -p "$WORKTREE"
git --work-tree="$WORKTREE" --git-dir="$PWD" checkout -f "$BRANCH"
log "checked out $deploy_sha into $WORKTREE"

# The bare repo mirrors the TARGET's repository, so the build lives at its root.
# Until 21 September 2026 PerfLab was a subdirectory of Crucible and this was
# "$WORKTREE/perf-lab"; the deploy branch then carried Crucible's source to a box
# that had no use for it (DESIGN.md 19.1b).
cd "$WORKTREE"
# -o would be nice for speed but a fresh box has no ~/.m2; leave it online.
./mvnw -B -DskipTests -Dperflab.commit="$deploy_sha" package
log "built perf-lab with commit=$deploy_sha"

# Stop the old JVM before starting the new one. Two JVMs on :8080 means the
# second fails to bind and the FIRST keeps serving -- Crucible would then measure
# the old build while the deploy reported success, which is exactly the failure
# 19.6 exists to catch. Better to be down for a moment than ambiguous.
# Stop EVERYTHING on the port before starting anything, and refuse to start if
# the port is still held.
#
# Two JVMs on :8080 means the second fails to bind and the FIRST keeps serving,
# so a deploy reports success while the old build answers every request. That is
# not hypothetical: on 20 September 2026 this hook started a build, saw "health
# UP" 0 seconds later, and was reading a stale instance left over from a manual
# run. The gate caught it (DEPLOY UNVERIFIED) -- but the gate is the last line
# of defence, not the first.
#
# The pid file alone is not enough, because an instance started by hand never
# wrote one. So: pid file first, then anything matching the jar, then verify the
# port is actually free.
if [ -f "$HOME/perflab.pid" ] && kill -0 "$(cat "$HOME/perflab.pid")" 2>/dev/null; then
  kill "$(cat "$HOME/perflab.pid")" 2>/dev/null || true
  log "signalled previous instance from pid file"
fi
pkill -f "perf-lab-0.1.0.jar" 2>/dev/null && log "signalled instances matching the jar" || true

for _ in $(seq 1 30); do
  curl -fsS --max-time 2 "$HEALTH_URL" >/dev/null 2>&1 || break
  sleep 1
done

if curl -fsS --max-time 2 "$HEALTH_URL" >/dev/null 2>&1; then
  log "ABORT: something is still serving $HEALTH_URL after 30s; refusing to start"
  log "      a second JVM would fail to bind and the OLD build would keep serving"
  exit 1
fi
log "port is free"

# NO --spring.datasource.hikari.maximum-pool-size HERE, and that is the whole
# point of the loop rather than an oversight.
#
# A Spring command-line argument OVERRIDES application.properties. The pool
# size is exactly what the agent edits in that file, so passing it here would
# mean every experiment was written, committed, pushed, built, deployed -- and
# then silently ignored, while the service ran on whatever this line said. The
# measurement would be of the OLD configuration attributed to the NEW change:
# the same class of error as measuring before a deploy lands (DESIGN.md 19.6),
# reached by a different route and just as plausible-looking.
#
# The K1 runs DID pass it on the command line, deliberately, so the value in
# force was recorded rather than inferred. That was right for a human driving
# runs by hand, and is wrong the moment an agent drives them: the properties
# file has to be the single source of truth for anything under experiment.
#
# setsid, because a post-receive hook exits as soon as the push completes and a
# plain background child can go with it -- observed on this box on 13 September
# 2026 when a backgrounded JVM died with its SSH session.
setsid nohup java -jar target/perf-lab-0.1.0.jar \
  --spring.profiles.active="$SPRING_PROFILE" \
  < /dev/null >> "$HOME/perflab.out" 2>&1 &
echo $! > "$HOME/perflab.pid"
log "started perf-lab pid $(cat "$HOME/perflab.pid") (pool size from application.properties)"

# Wait for health, then for the commit. Both, in that order: an unhealthy app can
# still answer /api/version from a half-initialised context, and a healthy one
# that reports the previous sha means the restart silently did not take.
for _ in $(seq 1 60); do
  if curl -fsS --max-time 3 "$HEALTH_URL" | grep -q '"status":"UP"'; then
    log "health UP"
    break
  fi
  sleep 2
done

for _ in $(seq 1 60); do
  reported="$(curl -fsS --max-time 3 "$VERSION_URL" | sed -n 's/.*"commit":"\([^"]*\)".*/\1/p')"
  if [ "$reported" = "$deploy_sha" ]; then
    log "deploy verified: /api/version reports $reported"
    exit 0
  fi
  sleep 2
done

log "DEPLOY UNVERIFIED: /api/version never reported $deploy_sha"
# Non-zero so the push itself reports failure. Crucible would refuse to measure
# regardless -- it polls /api/version itself and does not trust this hook's word
# -- but a push that silently "succeeded" would waste an operator's afternoon.
exit 1
