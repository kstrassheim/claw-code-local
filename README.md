# claw-code-local

GitOps deployment of [openclaw](https://github.com/openclaw/openclaw) as a
single-tenant coding agent on a local Kubernetes cluster, talking to
hosted LLM providers (Mistral primary, MiniMax optional) and driven
over Telegram.

The repository builds a custom openclaw image, ships the Kubernetes
manifests as a Kustomize bundle, and is reconciled into the cluster by
Argo CD. Secrets are **not** stored in the repo — the Deploy workflow
reads GitHub Actions environment secrets and `kubectl apply`s them
directly to the cluster on every run (the YAML never touches disk or
git).

## What's in the image

The container image (`builder/Dockerfile`) is `openclaw` upstream plus a
curated set of CLIs and MCP servers for autonomous code / cloud work:

- `git` (with `git-lfs`), `gh` + `github-mcp-server`
- `tea` + `gitea-mcp` (Gitea; inert unless `GITEA_URL` and `GITEA_API_TOKEN` are both set)
- `kubectl` + `kubelogin` + an in-house Kubernetes MCP (`builder/k8s-mcp`)
- `terraform` + the official Terraform MCP
- `aws`, `gcloud`, `aliyun` CLIs each paired with a cloud-specific MCP
  (`builder/aws-mcp`, `builder/gcp-mcp`, `builder/alicloud-mcp`)
- Code scanners — `semgrep`, `bandit`, `pip-audit`, `npm audit`,
  `gitleaks`, `PSScriptAnalyzer` — behind `builder/security-mcp`
- Live scanners — `nuclei` (pinned templates) and `testssl.sh` — behind
  `builder/pentest-mcp`. Deliberately **not** on `$PATH`: the server
  calls them by absolute path so no agent shell can invoke a scanner
  outside the scope enforcement.
- `pwsh` with Pester, the .NET SDK, SqlPackage, `sqlcmd`/`bcp` and four
  `Az` modules for data-platform work
- Entra ID TOTP helper (`builder/entra-totp`) for Azure CLI sign-in
  with MFA
- A debug MCP (`builder/debug-mcp`)
- `code-server` for an in-pod web IDE
- `pymongo`, the only pip package, for the planning store

Upstream's bundled skills are stripped against an **allowlist**
(`builder/BUNDLED_SKILLS_ALLOWED`, currently empty), not a denylist of
the known-dangerous ones — otherwise a new upstream skill ships to the
agent silently. The build then asserts what is actually loadable in the
finished image and fails on anything unlisted, because `rm -rf` on a
path upstream has moved exits 0 and removes nothing. At runtime the
skill directories are root-owned and read-only, so the agent's surface
area is exactly what is wired in `builder/` and described in
`builder/tools-md/`.

The full per-tool capability description lives in
[`builder/tools-md/`](builder/tools-md/) — those `.md` files are
concatenated, in the order given by `ORDER` beside them, into the
`TOOLS.md` the agent reads. They ship twice: baked into the image, and
as the `claw-tools-parts` ConfigMap which overlays them at runtime.
`render-config` assembles the document at pod start from whichever is
present, so a ConfigMap that fails to apply costs freshness rather than
leaving the agent with no capability list at all.

## Repository layout

```
builder/        Dockerfile, the runner scripts, and the ConfigMap
                generator that ships them without an image rebuild
  tools-md/     TOOLS-*.md capability docs + ORDER (assembled into
                the TOOLS.md the agent reads)
  heartbeat-issue-tick.py   Issue-solver planner
  cron-issue-spawn.sh       Issue-solver spawner
  fixer-runner.sh           Issue-solver runner
  tester-tick.py            Deployment-tester planner
  tester-runner.sh          Deployment-tester runner
  reviewer-tick.py          Pull-request reviewer planner
  reviewer-runner.sh        Pull-request reviewer runner
  issue_status.py           The five-value status model
  project-allow             The permission boundary (allowed repositories)
  planning_store.py         Planning documents: spool, then MongoDB
  agent-models/-limits/-thinking/-slot.sh   Runtime controls
  tests/        Standard-library unittest suite (no pytest, no pip)
  tools/        Repository checks run by CI
k8s/            Kustomize bundle deployed by Argo CD
  006-mongodb.yaml          Planning database, own pod and volume
  050-issue-watcher.yaml    Issue-solver CronJob, RBAC, chat skill
  051-tester.yaml           Deployment-tester CronJob, RBAC, chat skill
  052-reviewer.yaml         Reviewer CronJob, RBAC, chat skill
  053-projects.yaml         Permission chat skill
  054-planning.yaml         Planning / product-owner chat skill
argocd/         Argo CD AppProject + Applications
.github/
  workflows/    image build, secret apply, validation, CodeQL
VERSIONS        Pinned upstream versions (openclaw + every CLI baked in)
```

## How it deploys

```
                 push to main
                      |
              .github/workflows/deploy.yml
              /                            \
   publish-secrets  (direct apply)    build-and-push-image
              |                              |
              v                              v
       kubectl apply -f -          docker push + commit
       openclaw-secrets             k8s/kustomization.yaml
       (no git, no commit)         (image-tag pin)
                                         |
                                         v
                                  Argo CD auto-sync
                                         |
                            kustomize build k8s/  →  apply
                                         |
                              Pod up in `claw-code-local`
```

- `publish-secrets` reads GitHub Actions secrets and `kubectl apply`s
  the resulting `openclaw-secrets` Secret directly to the cluster.
  The manifest is piped from `kubectl create -o yaml` into
  `kubectl apply -f -` and never written to disk or git. Argo CD
  does **not** manage this Secret; the workflow is its sole owner.
- `build-and-push-image` resolves the upstream openclaw tag from
  `VERSIONS`, layers in the extra CLIs / MCP servers, pushes the
  result to a private registry, and commits a pinning update to
  `k8s/kustomization.yaml`'s `newTag:` so Argo CD picks up the new
  tag on the next reconcile.
- Argo CD watches `k8s/` (Kustomize) and auto-syncs. The scripts and the
  tools documents are generated into ConfigMaps from `builder/` by
  `builder/kustomization.yaml`, so editing one is a ConfigMap update
  rather than an image rebuild — no version bump, no pull, and for the
  scripts no restart either.

The destination namespace is `claw-code-local`. The Kustomize
`images:` override pins the openclaw image tag; the build workflow's
"Pin Image Tag" step keeps `newTag:` in
[`k8s/kustomization.yaml`](k8s/kustomization.yaml) in sync with
`OPENCLAW_VERSION`, so bumping `VERSIONS` is enough to roll a new
version end-to-end.

## Autonomous issue watcher

The cluster runs a `*/5 * * * *` CronJob in `claw-code-local` that
auto-fixes any GitHub issue assigned to the bot account. Each fixer
is an `openclaw agent --local` Node.js **subprocess spawned inside
the running openclaw pod**, not a separate Pod — so it inherits the
main pod's network, secrets, MCP servers, plugin registry, and
config by construction.

```
       CronJob issue-watcher           (own pod, every 5 min)
              |
       cron-issue-spawn (bash)
              |
       heartbeat-issue-tick (python)
       |                       \
GET /issues?filter=assigned     `kubectl exec openclaw-pod -- ls .fixer-locks/`
       \                       /
        \                     /
         decide toSpawn list  ←  cap at 1 active fixer per repo
                  |
        for each toSpawn entry:
        kubectl exec openclaw-pod -- nohup fixer-runner repo n url title &
                  |    (subprocess inside the openclaw container)
                  v
       fixer-runner:
         mkdir lock at ~/.openclaw/.fixer-locks/<owner>__<name>/
         clone-or-update ~/.openclaw/projects/<owner>/<name>/
         git checkout -b issue-<n>-fix
         openclaw agent --local --message "Fix issue …"
            → commit → push → open PR
         trap: rm -rf lock on exit
```

- **Concurrency ledger**: lock directories at
  `~/.openclaw/.fixer-locks/<owner>__<name>/` inside the openclaw
  pod. `mkdir` is atomic on local filesystems — the first runner
  that asks wins, everyone else exits fast. **Max 1 fixer per
  repo**, because the shared on-disk checkout can't be safely
  raced. Issues queued for a busy repo wait for the next tick.
- **Shared persistent checkout**: each repo has one working tree
  under `~/.openclaw/projects/<owner>/<name>/` on the openclaw
  PVC. Survives pod restarts, so the agent benefits from a warm
  `.git`, cached `node_modules`, etc.
- **TTL**: each fixer subprocess is bounded by the agent's
  `--timeout 3500` flag (~58 min). Stale locks older than 1h
  (planner-checked on every tick) are ignored, so a crashed fixer
  doesn't permanently hold a repo.
- **Coding agent**: same Node.js runtime as the chat bot, same
  rendered `~/.openclaw/openclaw.json` (MiniMax M2.7 primary,
  Mistral Large fallback), same MCP servers and skills.

The watcher CronJob, its service account, RBAC (the cron pod needs
`pods/exec` on the openclaw deployment's pods), and the chat-skill
ConfigMap are all in
[`k8s/050-issue-watcher.yaml`](k8s/050-issue-watcher.yaml).

### Controlling it from chat

The same manifest ships a `developer` skill (mounted at
`~/.openclaw/workspace/skills/developer/SKILL.md` via subPath
ConfigMap). The bot picks the skill up at session start and
recognises plain-text triggers:

| You type | What runs |
|---|---|
| `developer status` | `kubectl get cronjob issue-watcher -o jsonpath=…` |
| `developer start` | `kubectl patch cronjob issue-watcher … suspend:false` |
| `developer stop`  | `kubectl patch … suspend:true` AND `pkill -f 'openclaw agent --local'` AND `rm -rf $HOME/.openclaw/.fixer-locks/*` |
| `developer list`  | `ls $HOME/.openclaw/.fixer-locks/` (one line per active repo) |
| `developer logs <repo>#<n>` | `tail $HOME/.openclaw/fixer-logs/<owner>_<name>-<n>.log` |
| `developer kill`  | the second half of `stop` only — terminates in-flight fixers without suspending the CronJob |

The chat-facing skill name is `developer`; the underlying CronJob is
still called `issue-watcher` (and the lock dir is still
`.fixer-locks/`) — those are infrastructure names below the chat
surface.

`developer stop` deliberately kills in-flight subprocesses too —
partial work is discarded, because the user's intent on "stop" is
"stop coding work right now", not "finish what's in progress".

`spec.suspend` is *deliberately absent* from the CronJob manifest
(K8s defaults it to `false`). With Argo CD's ServerSideApply mode
that leaves the field unmanaged, so `kubectl patch … suspend:true`
from the chat skill survives reconciliation instead of being
self-healed back to running.

### Merge policy

The fixer's rule 7 is **default-allow merge**: once required CI is
green on the PR, the agent calls `merge_pull_request` itself and
the wrapper closes the issue. To opt a single issue out, put one
of `do not merge`, `don't merge`, `leave for review`, `manual
review only`, `no auto-merge`, or `hold for approval` somewhere in
the issue body — the fixer parses for those before merging.

Rule 12 forbids the agent from weakening any quality gate to get
CI green: no lowering coverage thresholds, no skipping/`xit`-ing
failing tests, no `// eslint-disable` / `@ts-ignore`, and no
editing `.github/workflows/**` to make a gate non-fatal (no
`|| true`, no `--check-coverage=false`, no
`continue-on-error: true`). Reaching that situation is a rule-5
LAST-RESORT — the agent comments on the issue with the concrete
numbers and waits for direction.

When CI on the PR is red, the wrapper pre-fetches the failing
job's log via the GitHub API and injects a condensed excerpt into
the agent's initial prompt under a `## Failing CI excerpt`
heading, so the agent diagnoses the actual error message instead
of guessing from the workflow YAML or asking the user to paste the
log.

### Disabling permanently

Suspend the CronJob via `developer stop` and don't unsuspend it.
To remove the watcher entirely, delete `050-issue-watcher.yaml`
from `k8s/kustomization.yaml` and let Argo CD prune the CronJob +
RBAC. Existing on-disk state under `~/.openclaw/projects/` and
`~/.openclaw/.fixer-locks/` is harmless to leave around.

## Autonomous deployment tester

A sibling CronJob `tester` (`*/10 * * * *`) watches the
default-branch HEAD of every repo the bot collaborates on. On each
tick, for any repo whose current HEAD differs from the last-tested
SHA on disk, it spawns a `tester-runner` subprocess inside the
openclaw pod — same pattern as the fixer, with a separate lock
directory (`~/.openclaw/.tester-locks/<owner>__<name>/`) so the two
subsystems never block each other.

The tester is the **inverse** of the fixer:

| | fixer (`developer`) | tester |
|---|---|---|
| Source of work | GitHub issues assigned to the bot | new default-branch HEAD |
| Mutation rights | branches + commits + PR + merge | none — no commits, no git push |
| Exit signal | PR merged / issue closed | issue staged + run summary |

Per-run flow:

1. **Pipeline check** — `github__list_workflow_runs` on the tested
   commit. Zero runs is treated as "workflows not configured for
   this push event", not a failure (the agent must not attribute
   sibling-commit CI to the tested SHA).
2. **Find a deployed URL** — search the local checkout (workflows,
   terraform, README, k8s manifests). Prefers `dev` env URLs.
3. **Browser open + autonomous Entra login** — uses the browser
   plugin (with per-tester `BROWSER_PROFILE` isolation) and the
   `ENTRA_USERNAME` / `ENTRA_PASSWORD` / `entra-totp` helpers to
   complete MSAL sign-in end-to-end with zero user interaction.
4. **Exercise the page** — navigate routes, fill forms, watch
   console + network. Distinct error classes get a draft each.
5. **Finalize** — print one summary line and the literal sentinel
   `TESTER_DONE <head_sha>`. The wrapper's sentinel watcher pkills
   the agent ~10s later and proceeds to issue creation.

Drafts staged during the run live in
`~/.openclaw/tester-drafts/<owner>__<name>-<sha>/` as one JSON file
each. The wrapper reads them after the agent exits, uploads any
referenced screenshots, and creates the GitHub issues with the
right assignee — `BOT` for code-fixable findings (auto-routed back
to the fixer subsystem), `OWNER` for things only a human can address
(infrastructure access denied, missing credentials, etc.).

Screenshots are uploaded to an orphan branch `tester-screenshots`
in the same repo (one folder per `<sha>`) and embedded inline in
the issue body via `raw.githubusercontent.com/.../tester-screenshots/...`
URLs. The branch is auto-created on first use and shares no history
with `main`.

On completion the wrapper posts the run summary as a GitHub commit
comment **and** sends it to Telegram via
`openclaw message send --channel telegram` (chat id resolved from
`commands.ownerAllowFrom` in the openclaw state file — no
hardcoded identity in the prompt or wrapper).

The full CronJob + chat skill for the tester is in
[`k8s/051-tester.yaml`](k8s/051-tester.yaml). Chat triggers mirror
the developer skill: `tester status`, `tester start`, `tester
stop`, `tester list`, `tester logs <repo>`, `tester last <repo>`.

## The permission boundary

All three autonomous subsystems discover their own work from account-wide
queries — issues assigned to the bot, pull requests it is asked to review,
repositories it collaborates on. Without a second gate, **anyone who can
assign the bot an issue can put it to work on any repository it can see**.

`~/.openclaw/projects-allowed.list` on the workspace volume is that gate.
Being assigned something is how a person *asks*; this list is where the owner
*answers*. Every planner and every runner re-checks it, and it is read fresh
on each tick, so a revoke takes effect within one tick without a redeploy.

Manage it from chat (the `projects` skill) or with the CLI:

```
project-allow list
project-allow add https://github.com/owner/repo --actor you
project-allow revoke owner/repo --actor you
project-allow check owner/repo          # exit 2 = not permitted
```

It **fails closed**: an unreadable or missing list permits nothing. `check`
and `bootstrap` make no network call at all, so a GitHub outage can never
become a permission decision, and the init container that seeds the list on
first boot never overwrites an existing one — otherwise a revoke would last
only until the next deploy.

## Issue status on a platform with two states

The solver needs five answers to "what is happening to this issue?", because
each leads somewhere different on the next tick. GitHub issues have `open` and
`closed`. The mapping:

| Status | How it is stored |
|---|---|
| To do | open, no status label (the default) |
| In progress | open, `status::in-progress` |
| Done | closed, `state_reason=completed` |
| Won't do | closed, `state_reason=not_planned` + `status::wont-do` |
| Duplicate | closed, `state_reason=not_planned` + `status::duplicate` |

The terminal pair uses GitHub's native close reason rather than a third label
because it records the operator's intent at the moment of closing: a delivered
issue and a revoked one stay distinguishable afterwards, instead of having to
be re-derived from merge history. Nothing on GitHub enforces one value per
label prefix, so the bot clears the previous status itself on every
transition, and writes nothing at all when the status has not changed — the
tick runs every five minutes and a no-op label write still appends a timeline
event.

## Autonomous pull-request reviewer

A third planner/runner pair (`k8s/052-reviewer.yaml`, CronJob `pr-reviewer`).
It lists open pull requests where the bot is a requested reviewer, and spawns
only when the head commit's checks are green and that exact head has not
already been reviewed. It reviews in its **own** checkout tree, and never
edits code, pushes, files issues or merges.

The verdict is one comment whose first line is
`🔎 REVIEW RESULT: APPROVED (sha <sha>)` or `CHANGES REQUIRED (sha <sha>)`,
followed by a real GitHub review. Three rules matter:

- **Green is read from both check-runs and commit statuses**, and "no checks
  at all" is kept distinct from "pending" — an empty combined status reports
  itself as pending, and believing it would strand every repository without
  CI forever.
- **The already-reviewed key is the head SHA plus a fingerprint of the title
  and body**, not the SHA alone, so a verdict about the *description* can be
  cleared by editing the description instead of pushing an empty commit.
- **A run that does not complete posts nothing** and retries next tick. A
  provider outage must not wedge a pull request as "changes required".

Security findings come from the code-scanning alerts for the pull request's
own head, thresholded by `security-level` (default `high`). An unreadable
threshold falls back to the default, never to `off`.

Suspending the reviewer CronJob is supported: the solver then merges green
pull requests directly, which is its pre-reviewer behaviour. The chat skill
says so on every `stop`.

## Planning, sprints and story points

`k8s/006-mongodb.yaml` runs a small MongoDB with its own pod and volume, and
`builder/planning_store.py` writes to it. Every write lands in an append-only
spool on the workspace volume **first** and flushes opportunistically, and
nothing in the store raises — a planning store that can kill a solver run
costs more than it will ever be worth. Document ids are deterministic and
every write is an upsert, so a document flushed twice is a no-op.

An unconfigured or unreachable store is a supported state: work continues and
spools. Query it through the `planning` CLI or the `planning` chat skill —
never directly.

Story size lives in an `SP::<n>` label and nowhere else. Sizing runs on the
cheap planning model one tick before implementation, so the solver can route
small stories to a cheaper model; a story at the split ceiling is parked
rather than started.

## What a pull request must pass

`.github/workflows/validate.yml` runs on every pull request and every push to
main:

| Job | What it fails on |
|---|---|
| `unit-tests` | any test in `builder/tests/` |
| `function-coverage` | a NEW untested function appearing |
| `check-python-names` | a name loaded but never bound |
| `check-model-config` | a silent regression in the model ConfigMap |
| `check-tools-docs` | a tools document not assembled, or truncated past the bootstrap cap |
| `check-llm-secrets` | no model provider configured |
| `verify-skills-locked` | the agent could gain an unreviewed capability |
| `verify-build` | the image not building, an unlisted bundled skill, or the runtime lockdown not holding. **Skipped when `OPENCLAW_VERSION` is unchanged** — the tag already exists, so there is no new image to verify |

The cheap checks run on hosted runners and the image build depends on all of
them, so a failing test costs about a minute rather than an arm64 image build.
On a push to main, Deploy waits for those same checks before building
anything: a commit whose tests fail produces no image, no push and no tag
on the single self-hosted scale set.

The test suite is standard-library `unittest` — no pytest and no pip
packages — because a suite that only runs where someone remembered to install
a framework is a suite that stops being run. Run it with:

```
cd builder/tests && python3 -m unittest discover -s . -p 'test_*.py'
```

## Prerequisites

The deploy target is assumed to provide:

- A Kubernetes cluster with Argo CD and a default StorageClass that
  provisions `ReadWriteOnce` volumes.
- A reachable container registry the cluster can pull from (image
  pull credentials are expected in a `registry-pull-secret` Secret in
  the target namespace — this is the only Secret not managed by the
  pipeline; see "Bootstrap" below).
- A self-hosted GitHub Actions runner that has kubectl reach into
  the target namespace (the workflows use
  `arc-runner-scale-claw-code-local`; its ServiceAccount must be
  granted `secrets: create/update/get/patch` on `claw-code-local`).

## Required GitHub Actions secrets and variables

Set on the repository (Settings → Secrets and variables → Actions).
The deploy workflow `kubectl apply`s every secret listed here as a
`Secret` named `openclaw-secrets` in the `claw-code-local` namespace
(directly to the cluster — never written to disk or committed).

**Secrets**

| Name | Used for |
|---|---|
| `MISTRAL_API_KEY` | Required. Primary model + image-model provider. |
| `MINIMAX_API_KEY` | Optional. Stripped at pod start if unset. |
| `TELEGRAM_BOT_TOKEN` | Telegram channel. Pair the bot with `openclaw pairing approve telegram <code>` after first start. |
| `BOT_GITHUB_TOKEN` | Sealed as `GITHUB_TOKEN`; PAT the agent uses for git/gh operations. |
| `GITLAB_TOKEN`, `GITLAB_LOCAL_TOKEN` | GitLab.com and self-hosted GitLab PATs. |
| `GITEA_URL`, `GITEA_API_TOKEN` | Optional. Base URL and API token of a Gitea instance. Set **both** and Gitea joins the hosts the forge layer works, alongside GitHub and GitLab; set neither and nothing changes. Half a pair is not a host. |
| `ENTRA_TENANT_ID`, `ENTRA_USERNAME`, `ENTRA_PASSWORD`, `ENTRA_TOTP_SEED` | Azure / Entra ID sign-in for the TOTP helper. |
| `TESTER_ALLOWED_HOSTNAMES` | Optional. Comma-separated LAN hostnames the tester's browser plugin may navigate to (private-network deploy URLs). Injected into `browser.ssrfPolicy.allowedHostnames` at pod start; kept in the Secret so the internal DNS domain stays out of this public repo. |
| `MOONSHOT_API_KEY` | Optional. Kimi Coding endpoint, the default model when present. |
| `PROJECTS_ALLOWED_BOOTSTRAP` | Optional. Comma-separated `owner/repo` list used to SEED the allowed-projects list the FIRST time a workspace volume comes up. Ignored on every later start, so a revoke is never undone by a deploy. Empty means the bot starts permitted on nothing, which is the correct default. |
| `INTERNAL_CA_CERT` | Optional. PEM of the internal CA that signs the cluster's HTTPS ingresses. The `fix-perms` init container imports it into Chromium's NSS store so the tester opens internal HTTPS deploy URLs without `ERR_CERT_AUTHORITY_INVALID`. Kept in the Secret so the internal CA stays out of this public repo. |

Missing optional secrets are tolerated: openclaw config strips Mistral
or MiniMax when its key is empty, and individual MCP servers fail
soft when their credentials aren't present.

## Bootstrap

For a fresh cluster, applied once out-of-band:

1. Argo CD AppProject + Applications: `kubectl apply -f argocd/`. The
   `app-of-apps.yaml` then materialises the rest.
2. `registry-pull-secret` in the target namespace, holding a
   `kubernetes.io/dockerconfigjson` for the image registry. This is
   referenced by the pod's `imagePullSecrets` and is the one piece of
   credential state not managed by the pipeline.
3. Push to `main` (or `workflow_dispatch` the Deploy workflow). The
   `publish-secrets` job creates `openclaw-secrets` directly via
   kubectl. From then on, every push to `main` re-applies it.

## When a version bump is required — and when it is not

Read this before editing anything in this repository. Bumping
`OPENCLAW_VERSION` costs a full image rebuild plus a ~1.8GB pull on the
node (four to five minutes during which the gateway is down and every
CronJob tick reports `no Running openclaw pod`). Most changes do not
need one, and bumping out of habit is the expensive mistake.

**The rule: bump only when the IMAGE itself must change.**

| You changed | Bump `OPENCLAW_VERSION`? | How it reaches the cluster |
| --- | --- | --- |
| `builder/*.py`, `builder/*.sh`, `forge-cli`, `mermaid-render` | **No** | ConfigMap → Argo → next CronJob tick |
| `builder/forge.py` + `builder/forge_{github,gitlab,gitea}.py` | **No** | same — and this is why the forge is FLAT sibling modules rather than a `forge/` package: a ConfigMap key cannot contain a slash, so a package could only reach a pod by rebuilding the image. |
| `builder/tools-md/*.md` (capability docs) | **No** | ConfigMap → Argo → next **pod restart** |
| `k8s/*.yaml` (manifests, skills, resources) | **No** | Argo applies them directly |
| `builder/Dockerfile` | **Yes** | nothing else rebuilds the image |
| A pin in `VERSIONS` (a CLI, MCP server, scanner, mermaid-cli…) | **Yes** | same |
| A **new** file under `builder/` | **Yes**, unless you also add it to `builder/kustomization.yaml` | see the trap below |
| `.github/workflows/*` | **No** | the workflow file is read per run |

### Why script edits need no bump

Everything under `builder/` ships **twice**: baked into the image, and
generated into ConfigMaps by
[`builder/kustomization.yaml`](builder/kustomization.yaml). The
ConfigMaps mount at `/opt/claw-scripts`, which comes first on both
`PATH` and `PYTHONPATH`, so the mounted copy wins.

That ordering is deliberate and gives three properties:

- **Edit → live without a rebuild.** Argo applies the ConfigMap and
  kubelet refreshes the files in place, no pod restart. Runners are
  spawned fresh per tick, so the next tick runs the new code.
- **A failed mount degrades to stale, never to missing.** The image's
  copy is still there. Nothing is ever absent.
- **A ConfigMap can add a file the image does not have.** Useful for a
  brand-new script; the image catches up at the next bump.

The tools documents work the same way, with one difference: they are
assembled into a single `TOOLS.md` by `render-config` at pod start, so
they land on the next **restart** rather than the next tick.

### The trap: a new file under `builder/`

Adding a file does not automatically put it in a ConfigMap. If you add
`builder/my-helper.sh` and only add the `COPY` to the Dockerfile, it
ships **only** in the image and therefore needs a bump — and every later
edit to it needs another one. Add it to `builder/kustomization.yaml` as
well, under whichever half runs it:

- `claw-scripts-planner` — anything a CronJob executes, and the modules
  those import
- `claw-scripts-runner` — the long-running runners and the libraries
  they `_source_lib`

Use the **installed** name as the key (`my-helper=my-helper.sh`) so the
same name resolves whichever copy wins. `tests/test_scripts_from_configmap.py`
checks the wiring; `tests/test_cron_image.py` re-derives what the cron
image must carry and fails if the two drift.

CI-only scripts (`verify-skills-locked.sh`, `verify-lockdown-effective.sh`,
`probe-skill-dirs.sh`) belong in neither — they run against the checkout
and the built image, never in a pod.

### What happens when you do bump

`OPENCLAW_VERSION` is the image tag *and* the cache key. The build is
skipped outright when the tag already exists — in the Deploy workflow
and in the pull-request image check — so an unchanged version costs
nothing in either place. A new value rebuilds and re-pushes both the
agent image and the CronJob image, which share the tag on purpose: they
carry the same scripts and must not drift.

Other pins in [`VERSIONS`](VERSIONS) (a CLI, an MCP server, a scanner)
also require an `OPENCLAW_VERSION` bump to ship — changing the pin alone
rebuilds nothing, because the tag has not moved.

`workflow_dispatch` accepts an optional `git_ref` input to build any
upstream openclaw tag/branch/commit without editing `VERSIONS`.

### Checking what is actually live

The deployed commit is recorded by Argo, not inferred:

```
kubectl get application -n argocd claw-code-local-manifests \
  -o jsonpath='{.status.sync.revision}'
```

For the image specifically, read the tag off the workload:

```
kubectl get deploy -n claw-code-local openclaw \
  -o jsonpath='{.spec.template.spec.containers[0].image}'
```

A script change moves the first and leaves the second alone. That is the
normal, healthy case — not a sign the deploy failed.

## License

GPL-3.0. See [`LICENSE`](LICENSE).
