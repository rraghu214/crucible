# SECURITY.md — Crucible

How Crucible is protected as deployed for the capstone, what was verified, what
was fixed, and what is knowingly left for later. The working analysis behind
this page, with every finding, file reference and test, is
[`THREAT_MODEL.md`](THREAT_MODEL.md). Method and vocabulary follow EAG V3
Session 12: eight security invariants, four attacker roles (R1 outsider, R2
text-only, R3 compromised component, R4 code inside the process), STRIDE.

Reviewed 22–25 October 2026: a static read of the code, a scanner run, 25
in-process reproduction tests, three prompt-injection probes against the hosted
gateway, and read-only checks of the two live boxes.

## 1 · The deployment this page describes

Crucible edits configuration on a running service and restarts it, so its whole
safety argument assumes a **pre-prod target, one operator, and no production
data** (DESIGN.md §8, §19.1). Within that:

| Component | Where | How it is reached |
|---|---|---|
| Crucible API and dashboard (`crucible serve`) | Box B, bound to `127.0.0.1:8113` | Only through the operator's SSH tunnel: `ssh -L 9113:127.0.0.1:8113 ubuntu@<box-b-ip>`, then `http://localhost:9113/dashboard` |
| Target service (perf-lab) | Box A, port 8080 | Only from Box B's private address (cloud NSG **and** host firewall) |
| Model gateway (glc_v5) | Hosted on Render | HTTPS from Box B |
| Everything else | — | From the internet, only SSH (22) answers on either box |

**This exposure model is the main control.** Several routes in the API do not
check the control token (§4). They are unreachable from outside because the
server binds loopback and no firewall rule admits its port. Anyone changing the
bind to `0.0.0.0`, or adding a firewall rule for 8113, must first close those
routes.

## 2 · Controls that hold (verified)

- **Control-plane auth fails closed.** Every campaign route that starts, approves,
  pauses, aborts, launches, uploads or writes the SLA requires
  `CRUCIBLE_CONTROL_TOKEN` (constant-time compare, header only). With the token
  unset they return 503 rather than serving everyone. The exceptions are routes
  inherited from the general agent, listed in §5.
- **The agent cannot move its own goal.** The guard refuses any change outside
  the profile's `allowed_properties` and their bounds; the SLA and load profile
  are protected paths (DESIGN.md §4.4).
- **A human approves each change** (Semi-Automated) and the deploy writes only
  to the sandbox branch: protected branches are refused when the profile loads,
  force-push is refused, and the refspec comes from configuration, never from
  the model.
- **No measurement before the change is proved in force:** the target must
  report the deployed commit (DESIGN.md §19.6).
- **Secrets stay out of the model's view.** Snapshot redaction is an allowlist;
  perf-lab's `/actuator/env` masks every value except the twelve tunables
  Crucible changes, and a test pins the Datadog key as masked.
- **Model output is sanitised before the browser.** The chat reply is escaped
  and only seven attribute-free tags are re-allowed.
- **Hooks never see Crucible's secrets.** They run as `python -I` with no
  shell and an allowlisted environment that excludes every `CRUCIBLE_*` name.
- **The deploy key cannot open a shell** on Box A (restricted to `git-shell`,
  no forwarding).
- **No dangerous calls:** no `shell=True`, `eval`/`exec`, pickle, unsafe YAML,
  or disabled TLS verification anywhere in the package.
- **No secrets in git history** (scanned for the common key formats).

## 3 · Invariant scorecard, for this deployment

| # | Invariant | Status | Why |
|---|---|---|---|
| 1 | No component holds a credential it does not need | Partial | Hooks and the model never see secrets; but all of Crucible's tokens share one process environment. |
| 2 | Every action is authorised against the real user and exact arguments | Holds for the deployment | Writes need the token; the unauthenticated routes are loopback-only (§1). |
| 3 | Retrieved content never gains instruction authority | Partial | The chat reply is sanitised; two dashboard fields (approval card, history) are not (backlog). The model refused all three injection probes. |
| 4 | A credential works for one action only | Open | Approval decisions carry no nonce or expiry (backlog). |
| 5 | Memory is partitioned, with provenance | Partial | Partitioned by scope; the unauthenticated search route lets a local caller choose the scope. |
| 6 | High-impact actions are approved with their final parameters | Partial | The approval binds the property and value; it does not bind a hash of the final diff (backlog). |
| 7 | No component edits its own audit history | Open | The audit log is append-only by convention, not hash-chained (backlog). |
| 8 | Enforceable time, token, tool-call and cost budgets | Holds | Per-run budget, cost and wall-clock caps, a three-miss stop; negative usage cannot lower spend. |

"Open" items all need an attacker who already has code running on Box B (R4),
which the single-operator pre-prod deployment does not model. They are listed
below so that a move to a shared or exposed deployment starts from them.

## 4 · Fixed during the review

| Fix | Commit |
|---|---|
| A masked `/actuator/env` value is reported as "read-back unavailable", not as a deploy mismatch | `10ccfdf` |
| perf-lab shows only Crucible's tunables in `/actuator/env`; everything else stays masked | perf-lab `8af82d6` |
| Box IPs, the operator's IP, local paths, key file names and the gateway URL removed from the public repo (real values in a git-ignored file) | `92506b6` |
| Box B: the `.env` holding the tokens and the state directory are readable by their owner only | box change |
| `POST /v1/agent/documents` requires the control token; chat replies are sanitised; hooks get an allowlisted environment | landed during the review period |

## 5 · Known limitations and backlog

Ordered by what to do first if the deployment ever becomes shared or exposed.

| Priority | Item | Invariant | Why it can wait for the capstone | Fix |
|---|---|---|---|---|
| 1 | **The hosted gateway accepts `/v1/chat` without a token**, and its URL is in public git history | 8 | Free-tier keys with provider quota caps; trial infrastructure | Turn on gateway auth (Crucible already sends `CRUCIBLE_GATEWAY_TOKEN`) |
| 2 | `/v1/action`, `/v1/perf/chat`, `/v1/agent/facts`, `/v1/agent/memory/search`, `/a2a` and the read routes need no token | 2, 5 | Loopback-only (§1) | Add `require_control`; make A2A fail closed; turn `/docs` off outside development |
| 3 | Box A's deploy hook builds whatever is pushed, and the repo does not itself refuse force-pushes | 6 | Only Box B holds the (restricted) deploy key | Server-side diff check in the hook; `receive.denyNonFastForwards` / `denyDeletes` |
| 4 | An uploaded hook runs as Crucible's OS user, which has passwordless sudo | 1 | No hook is in use; uploading one needs the control token | Run hooks as a separate low-privilege user, or refuse to run them when Crucible is privileged |
| 5 | A mid-run edit to `slo.yaml` on disk is accepted as the operator's | 2, §4.4 | Needs write access on Box B | Require the token or a signed amendment for mid-run edits |
| 6 | The push sends the whole branch history; the approval binds values, not a diff hash; the value written can differ from the value checked (e.g. `20.9` for an int) | 6 | R4 only; today's properties are all int/bool | Push only the approved commit; hash the diff at approval and re-check before push; write the coerced value |
| 7 | Approval files are unsigned, reusable and have no expiry | 4 | R4 only | HMAC-signed, single-use decisions bound to run, experiment and diff hash |
| 8 | The audit log and manifests can be edited | 7 | R4 only | Hash-chain the log; write manifests once |
| 9 | Two dashboard fields render model text as HTML; the token is kept in `localStorage` | 3 | The page is reached only through the tunnel | Escape those fields; keep the token in memory |
| 10 | Outbound URLs (agent tools, A2A push, trace export) have no allowlist | 1, 3 | The general-agent tools are unused by campaigns | One egress allowlist with private-address blocking, or disable those tools in the perf deployment |
| 11 | Deploy verification reads the property from `/actuator/env` rather than as a metric | principle 1 | Correct since the masking fix; the commit check is the gate | Compare the post-deploy snapshot's metric (e.g. `pool_max_connections`) with the applied value |
| 12 | Dependencies: 20 advisories across 8 locked packages, all with fixes; CI actions pinned to tags; no security scanners in CI | supply chain | No confirmed reachable exploit | Bump the lock; pin actions to SHAs; add bandit, pip-audit and semgrep |

## 6 · How to check this yourself

```bash
uv run pytest tests_security        # 25 reproduction and regression tests; no network
uvx bandit -r crucible scripts
uv export --frozen --no-hashes > req.txt && uvx pip-audit -r req.txt
```

`tests_security/` sits outside `testpaths`, so `uv run pytest -q` does not run
it. Each file's header names the finding it demonstrates and the question its
reviewer should ask. A test that asserts a weakness flips to assert the control
once the fix lands.

## 7 · Reporting

This is a capstone project, not a supported product. Report a security issue
privately to the repository owner through GitHub rather than in a public issue.
