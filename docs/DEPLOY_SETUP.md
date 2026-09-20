# Deploy setup — the push-to-deploy path

**Status: COMPLETE and verified end to end, 21 September 2026.** Box B pushes to
a bare repo on Box A over a dedicated git-only key; Box A's `post-receive` hook
rebuilds with the commit baked in, restarts, and the target proves itself.

This records what was executed, not what was planned. Where the first attempt was
wrong, the wrong version is called out rather than quietly replaced — the wrong
turns are the part worth keeping.

`docs/CLOUD_PROVISIONING.md` is the week-1 record and still describes the layout
as it was then, when PerfLab lived inside the Crucible repo. It has not been
rewritten. This document supersedes its §7 and §10 for anything deploy-related.

---

## What changed first: PerfLab is its own repository

**https://github.com/rraghu214/perf-lab**, split out of Crucible on 21 September
2026 with `git subtree split --prefix=perf-lab`, so its five original commits are
intact rather than squashed.

Why it had to move before any of this made sense (`DESIGN.md` §19.1b): while the
target lived inside Crucible, the deploy branch descended from Crucible's working
branch, so its tree carried Crucible's own source and Box A received a copy of
the tool that was testing it. Inert — the hook built one subdirectory — but
wrong-shaped, and two copies of a target drift apart the moment somebody edits
the wrong one.

Consequences for configuration, all of which bit during setup:

| | Before | After |
|---|---|---|
| `config_file` | `perf-lab/src/main/resources/application.properties` | `src/main/resources/application.properties` |
| `deploy.base_branch` | `capstone/perf-agent` | `main` |
| Hook build directory | `$WORKTREE/perf-lab` | `$WORKTREE` |
| Workspace | the Crucible checkout | a **perf-lab** checkout |

`config_file` is relative to `--workspace`, and the workspace is a checkout of
the *target's* repository. That is the whole seam.

---

## The shape

```
Box B (Crucible + Locust)                 Box A (target)
┌──────────────────────────┐              ┌──────────────────────────────┐
│ ~/perf-lab   (workspace) │              │ ~/perflab-deploy.git  (bare) │
│   agent edits            │  git push    │   post-receive hook          │
│   application.properties │ ───────────► │     ├─ checkout              │
│   commits one file       │  deploy key  │     ├─ ./mvnw package        │
│                          │  (git-only)  │     │    -Dperflab.commit=…  │
│ ~/crucible               │              │     ├─ stop old, verify port │
│   campaign, diagnosis    │              │     ├─ start new             │
└──────────────────────────┘              │     └─ poll /api/version     │
                                          │ ~/perflab-work (worktree)    │
                                          └──────────────────────────────┘
```

Crucible's only capability over Box A is **append a commit to a git repo**. It
holds no shell access, which is §19.8's goal: *"Where CI performs the deploy,
Crucible holds no credential for the box at all."*

---

## 1 · The deploy key

Generated on the operator's machine and stored in
`infra/OCI/Box-2/perflab-deploy-2026-09-21.key` (Box-2 because Box B is what
holds it). That directory is **outside any git repository** — checked before
writing, because a private key in a repo is unrecoverable once pushed.

```bash
ssh-keygen -t ed25519 -f ./perflab-deploy-2026-09-21.key -N "" \
  -C "crucible-deploy-boxb-to-boxa"
```

Fingerprint: `SHA256:fXzcKiL5ssSoSfTR2Vw5KHHo9CzlPooDnkhVmfJEd54`

It is dedicated, so revoking it costs nothing else. Nothing about the campaign
depends on the operator's personal key.

## 2 · Authorise it on Box A, git and nothing else

Appended to `~/.ssh/authorized_keys` on Box A as **one line**, restrictions
first:

```
command="git-shell -c \"$SSH_ORIGINAL_COMMAND\"",no-agent-forwarding,no-port-forwarding,no-pty,no-user-rc,no-X11-forwarding ssh-ed25519 AAAAC3Nza… crucible-deploy-boxb-to-boxa
```

`git-shell` accepts `git-receive-pack` and `git-upload-pack` and refuses
everything else. This is what "write-only deploy key" means when there is no
GitHub in the middle — the restriction is enforced by the *server*, not by the
holder's good behaviour.

**Verified, not assumed:**

```
$ git ls-remote ubuntu@10.0.0.79:perflab-deploy.git
7d1a8f51…  refs/heads/main                                    ← git works

$ ssh … ubuntu@10.0.0.79 'whoami; cat ~/.ssh/authorized_keys'
fatal: unrecognized command 'whoami; cat ~/.ssh/authorized_keys'   ← no shell
```

## 3 · Box A: target clone, bare repo, hook

```bash
git clone https://github.com/rraghu214/perf-lab.git ~/perf-lab
git init --bare ~/perflab-deploy.git
cp <crucible>/scripts/box_a_post_receive.sh ~/perflab-deploy.git/hooks/post-receive
chmod +x ~/perflab-deploy.git/hooks/post-receive

# seed it, so the sandbox branch has a base to be created from
git -C ~/perf-lab push ~/perflab-deploy.git main:refs/heads/main
```

The bare repo mirrors **the target only**. Box A never sees Crucible's source.

## 4 · Box B: key, alias, workspace, remote

```bash
# private half
chmod 600 ~/.ssh/perflab_deploy

# ~/.ssh/config — the remote URL carries a host ALIAS, never a credential.
# §19.8: a key path is safe to log, a token in a URL is not, and journals get
# replayed hundreds of times.
Host perflab-boxa
    HostName 10.0.0.79
    User ubuntu
    IdentityFile ~/.ssh/perflab_deploy
    IdentitiesOnly yes

# the workspace is a checkout of the TARGET
git clone https://github.com/rraghu214/perf-lab.git ~/perf-lab
cd ~/perf-lab
git remote add perftest perflab-boxa:perflab-deploy.git
```

`IdentitiesOnly yes` is not optional. Without it SSH offers every key it holds
and may authenticate as the operator instead — which would silently defeat the
restriction and look like success.

## 5 · Flip the profile

`config/profiles/spring-boot.yaml`:

```yaml
deploy:
  mode: pipeline          # only once the hook actually exists (§19.5)
  remote: perftest
  branch: perftest_sandbox
  base_branch: main
```

`mode` stayed `manual` until step 3 was done. §19.5: automation is declared,
never guessed — an agent assuming a pipeline would skip a deploy that never
happened and then measure the old build.

The sandbox branch does not need creating by hand. On first deploy Crucible
creates it **from `main`** and never writes to `main`:

```
branch exists on remote? False
created perftest_sandbox on perftest from main at 7d1a8f514852
```

---

## Two failures found during setup, both now fixed in the hook

### The hook was overriding the thing under experiment

The original hook passed `--spring.datasource.hikari.maximum-pool-size` on the
command line, inherited from the K1 runs. **A Spring command-line argument
overrides `application.properties`** — which is the file the agent edits. Every
experiment would have been written, committed, pushed, built, deployed, and then
silently ignored while the service ran on whatever the hook said. The measurement
would have been of the old configuration, attributed to the new change.

Passing it on the command line was *right* for K1, where a human drove the runs
and wanted the value in force recorded rather than inferred. It becomes wrong the
moment an agent drives them: the properties file has to be the single source of
truth for anything under experiment.

The hook now passes only `--spring.profiles.active`, and the deploy log says so:

```
started perf-lab pid 1070698 (pool size from application.properties)
```

Confirmed from the target: `"poolSize":10`, read from the file.

### A stale JVM kept the port and the new build never bound

First real deploy logged `health UP` **0 seconds** after start — impossible for a
cold JVM. An instance left over from an earlier manual run still held `:8080`;
the new one failed to bind and died, and the health probe was reading the old
process.

The gate caught it — `DEPLOY UNVERIFIED: /api/version never reported 7d1a8f51…`
— which is the design working. But a gate is the last line of defence, not the
first, and the stop logic was too weak: it killed only by pid file, and an
instance started by hand never wrote one.

The hook now kills by pid file, then by jar pattern, then **verifies the port is
actually free and refuses to start if it is not**:

```
port is free
started perf-lab pid 1070698
health UP
deploy verified: /api/version reports 52ea8ed1ee062ca83f36b50400047de25a11a756
```

Refusing to start is the important half. Starting a second JVM on a held port
produces the one genuinely ambiguous state: a deploy that reports success while
the old build answers every request.

---

## Verifying the whole path

```bash
# on Box B
cd ~/perf-lab
git commit --allow-empty -m "deploy check"
git push perftest HEAD:refs/heads/perftest_sandbox

# on Box A
tail ~/perflab-deploy.log
curl -s http://127.0.0.1:8080/api/version
```

A healthy run ends with `deploy verified` and a matching `commit` field.
Crucible does **not** trust the hook's exit code — it polls `/api/version`
itself, because a hook reporting its own success is evidence about the hook.
