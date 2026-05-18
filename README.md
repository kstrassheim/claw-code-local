# claw-code-local

GitOps deployment of [openclaw](https://github.com/openclaw/openclaw) as a
single-tenant coding agent on a local Kubernetes cluster, talking to
hosted LLM providers (Mistral primary, MiniMax optional) and driven
over Telegram.

The repository builds a custom openclaw image, ships the Kubernetes
manifests as a Kustomize bundle, and is reconciled into the cluster by
Argo CD. Secrets are committed encrypted with
[Sealed Secrets](https://github.com/bitnami-labs/sealed-secrets).

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
  workflows/    image build, sealed-secret rotation, validation, CodeQL
VERSIONS        Pinned upstream versions (openclaw + every CLI baked in)
```

## How it deploys

```
                 push to main
                      |
              .github/workflows/deploy.yml
              /                            \
   publish-secrets  (re-seals)        build-and-push-image
              \                            /
               \                          /
                \                        /
                 commit to main  ←  builds + pushes
                 (sealed-secrets         to local registry
                  if rotated)
                          |
                 Argo CD auto-sync
                          |
              kustomize build k8s/  →  apply
                          |
                Pod up in `claw-code-local`
```

- `publish-secrets` reads GitHub Actions secrets, runs `kubeseal`
  against the cluster's Sealed Secrets controller cert, and commits
  the encrypted YAML back to `main`.
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
auto-fixes any GitHub issue assigned to the bot account — no LLM
calls on idle ticks, no per-tick chat traffic. The architecture is
deliberately split so concurrency lives in the K8s control plane,
not the agent's prompt:

```
       CronJob issue-watcher
       (every 5 min)
              |
       cron-issue-spawn (bash)
              |
       heartbeat-issue-tick (python)
       |                       |
GET /issues?filter=assigned    GET batch/v1/jobs?label=app=issue-fixer
       \                       /
        \                     /
         decide toSpawn list  ←  cap at 2 active Jobs per repo
                  |
       for each toSpawn entry:
       kubectl create job fix-<repo>-<#>-<ts>
                  |
       Job runs `openclaw agent --local --timeout 3600 …`
                  |
       clone → branch → code → commit → push → open PR → exit
```

- **Concurrency ledger**: K8s Jobs themselves. Each fixer Job is
  labeled `app=issue-fixer, issueRepo=<slug>, issueNumber=<n>`; the
  planner counts active ones per repo and only spawns within the
  per-repo cap (default 2). Jobs older than 1h with no completion
  are treated as dead — slots free up automatically.
- **TTL**: each spawned Job sets `activeDeadlineSeconds: 3700` (1h
  agent budget + 100s grace) and `ttlSecondsAfterFinished: 600`, so
  finished Jobs disappear ten minutes after they exit.
- **Coding agent**: each fixer pod runs `openclaw agent --local`
  with the same image, secrets, and rendered openclaw config the
  main bot uses. The default model (MiniMax M2.7) and tool registry
  (gh-issues skill, github-mcp-server, gh, git, etc.) are identical.
- **Workspace isolation**: each fixer Pod gets a fresh `emptyDir`
  for its `~/.openclaw` — no contention with the main bot's PVC and
  no cross-pollution between concurrent fixers.

The watcher is wired up in
[`k8s/050-issue-watcher.yaml`](k8s/050-issue-watcher.yaml): the
CronJob, its service account, the namespace-scoped Role granting
`jobs/create,list` for spawning fixers, and a separate
`issue-watcher-control` Role granting the main bot the
`cronjobs/patch` and `jobs/delete` it needs for the chat skill below.

### Controlling it from chat

The same manifest ships an `issue-watcher` skill (mounted at
`~/.openclaw/workspace/skills/issue-watcher/SKILL.md` via subPath
ConfigMap). The bot picks the skill up at session start and
recognises plain-text triggers:

| You type | What runs |
|---|---|
| `watcher status` | `kubectl get cronjob issue-watcher -o jsonpath=…` |
| `watcher start` | `kubectl patch cronjob issue-watcher … suspend:false` |
| `watcher stop` | `kubectl patch … suspend:true` AND `kubectl delete jobs -l app=issue-fixer` |
| `watcher list` | `kubectl get jobs -l app=issue-fixer` |
| `watcher kill` | only deletes in-flight fixers; CronJob stays scheduled |

`watcher stop` deliberately kills in-flight fixer pods too — partial
work is discarded, because the user's intent on "stop" is "stop
coding work right now", not "finish what's in progress".

`spec.suspend` is *deliberately absent* from the CronJob manifest
(K8s defaults it to `false`). With Argo CD's ServerSideApply mode
that leaves the field unmanaged, so `kubectl patch … suspend:true`
from the chat skill survives reconciliation instead of being
self-healed back to running.

### Disabling permanently

Suspend the CronJob and don't unsuspend it; or set `replicas` of the
CronJob's parent Application to 0 in
[`argocd/apps/claw-code-local.yaml`](argocd/apps/) (heavy-handed —
also deactivates the bot). Removing the manifest entirely is the
cleanest path if you don't want the watcher at all: delete
`050-issue-watcher.yaml` from `k8s/kustomization.yaml` and Argo CD
will prune the CronJob + its RBAC.

## Prerequisites

The deploy target is assumed to provide:

- A Kubernetes cluster with Argo CD, Sealed Secrets controller, and a
  default StorageClass that provisions `ReadWriteOnce` volumes.
- A reachable container registry the cluster can pull from (image
  pull credentials are expected in a `registry-pull-secret` Secret in
  the target namespace — this is the only Secret not managed by the
  pipeline; see "Bootstrap" below).
- A self-hosted GitHub Actions runner that can reach the cluster
  (the workflows use `arc-runner-scale-claw-code-local`). Workflows
  rely on in-cluster network reach for `kubeseal --fetch-cert`.

## Required GitHub Actions secrets and variables

Set on the repository (Settings → Secrets and variables → Actions).
The deploy workflow seals every secret listed here into the cluster
Secret `openclaw-secrets`.

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
   credential state not managed through Sealed Secrets.
3. The Sealed Secrets controller in `kube-system` — once present,
   pushing to `main` (or running the Deploy workflow manually) fills
   in everything else.

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
