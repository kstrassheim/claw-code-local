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

- `git`, `gh` + `github-mcp-server`, `glab` + a GitLab MCP
- `kubectl` + an in-house Kubernetes MCP (`builder/k8s-mcp`)
- `terraform` + the official Terraform MCP
- `aws`, `gcloud`, `aliyun` CLIs each paired with a cloud-specific MCP
  (`builder/aws-mcp`, `builder/gcp-mcp`, `builder/alicloud-mcp`)
- Entra ID TOTP helper (`builder/entra-totp`) for Azure CLI sign-in
  with MFA
- A debug MCP (`builder/debug-mcp`)
- `code-server` for an in-pod web IDE

The upstream `mcporter` and `skill-creator` skills are deliberately
removed so the agent's surface area is exactly what's wired in
`builder/` and described in `k8s/tools/`.

The full per-tool capability description lives in
[`k8s/tools/`](k8s/tools/) — those `.md` files are concatenated at
deploy time into a `TOOLS.md` ConfigMap and mounted into the pod, so
the agent's "what can I do" answer matches the deployment exactly.

## Repository layout

```
builder/        Dockerfile and per-MCP source for the openclaw image
  heartbeat-issue-tick.py   Issue-watcher planner (see below)
  cron-issue-spawn.sh       Issue-watcher Job-spawner (see below)
k8s/            Kustomize bundle deployed by Argo CD
  tools/        TOOLS-*.md fragments concatenated into TOOLS.md
  050-issue-watcher.yaml    Issue-watcher CronJob, RBAC, chat skill
argocd/         Argo CD AppProject + Applications + PreSync hook
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
- Argo CD watches `k8s/` (Kustomize) and auto-syncs. The PreSync hook
  in [`argocd/hooks/`](argocd/hooks/) regenerates the `openclaw-tools-md`
  ConfigMap from `k8s/tools/` and rolls the pod when the assembled
  TOOLS.md changes.

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

The same manifest ships an `issue-watcher` skill (mounted at
`~/.openclaw/workspace/skills/issue-watcher/SKILL.md` via subPath
ConfigMap). The bot picks the skill up at session start and
recognises plain-text triggers:

| You type | What runs |
|---|---|
| `watcher status` | `kubectl get cronjob issue-watcher -o jsonpath=…` |
| `watcher start` | `kubectl patch cronjob issue-watcher … suspend:false` |
| `watcher stop`  | `kubectl patch … suspend:true` AND `pkill -f 'openclaw agent --local'` AND `rm -rf $HOME/.openclaw/.fixer-locks/*` |
| `watcher list`  | `ls $HOME/.openclaw/.fixer-locks/` (one line per active repo) |
| `watcher logs <repo>#<n>` | `tail $HOME/.openclaw/fixer-logs/<owner>_<name>-<n>.log` |
| `watcher kill`  | the second half of `stop` only — terminates in-flight fixers without suspending the CronJob |

`watcher stop` deliberately kills in-flight subprocesses too —
partial work is discarded, because the user's intent on "stop" is
"stop coding work right now", not "finish what's in progress".

`spec.suspend` is *deliberately absent* from the CronJob manifest
(K8s defaults it to `false`). With Argo CD's ServerSideApply mode
that leaves the field unmanaged, so `kubectl patch … suspend:true`
from the chat skill survives reconciliation instead of being
self-healed back to running.

### Disabling permanently

Suspend the CronJob via `watcher stop` and don't unsuspend it. To
remove the watcher entirely, delete `050-issue-watcher.yaml` from
`k8s/kustomization.yaml` and let Argo CD prune the CronJob + RBAC.
Existing on-disk state under `~/.openclaw/projects/` and
`~/.openclaw/.fixer-locks/` is harmless to leave around.

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
| `ENTRA_TENANT_ID`, `ENTRA_USERNAME`, `ENTRA_PASSWORD`, `ENTRA_TOTP_SEED` | Azure / Entra ID sign-in for the TOTP helper. |

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

## Bumping versions

Everything pinned lives in [`VERSIONS`](VERSIONS). Common cases:

- New openclaw release: bump `OPENCLAW_UPSTREAM` and
  `OPENCLAW_VERSION`. The build workflow's "Pin Image Tag" step
  updates `k8s/kustomization.yaml` automatically — no manual edit.
- New CLI version (gh, glab, terraform, aws, gcloud, aliyun,
  code-server): bump the corresponding entry; the workflow rebuilds
  the image and pushes a fresh tag.

Bumping `OPENCLAW_VERSION` is also how you ship updates to the
issue-watcher wrapper scripts under `builder/` — they're baked into
the image, so a new tag is needed for them to land.

`workflow_dispatch` accepts an optional `git_ref` input to build any
upstream openclaw tag/branch/commit without editing `VERSIONS`.

## License

GPL-3.0. See [`LICENSE`](LICENSE).
