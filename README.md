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
k8s/            Kustomize bundle deployed by Argo CD
  tools/        TOOLS-*.md fragments concatenated into TOOLS.md
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
  `VERSIONS`, layers in the extra CLIs / MCP servers, and pushes the
  result to a private registry.
- Argo CD watches `k8s/` (Kustomize) and auto-syncs. The PreSync hook
  in [`argocd/hooks/`](argocd/hooks/) regenerates the `openclaw-tools-md`
  ConfigMap from `k8s/tools/` and rolls the pod when the assembled
  TOOLS.md changes.

The destination namespace is `claw-code-local`. The Kustomize
`images:` override pins the openclaw image tag, so bumping
`OPENCLAW_VERSION` in [`VERSIONS`](VERSIONS) and the matching
`newTag:` in [`k8s/kustomization.yaml`](k8s/kustomization.yaml) is the
canonical way to roll a new version.

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
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | AWS CLI / MCP. |
| `ALIBABA_CLOUD_ACCESS_KEY_ID`, `ALIBABA_CLOUD_ACCESS_KEY_SECRET` | Alibaba Cloud CLI / MCP. |
| `ENTRA_TENANT_ID`, `ENTRA_USERNAME`, `ENTRA_PASSWORD`, `ENTRA_TOTP_SEED` | Azure / Entra ID sign-in for the TOTP helper. |
| `ARGOCD_AUTH_TOKEN` | Argo CD MCP authentication. |

**Variables** (non-secret, repo Variables)

| Name | Used for |
|---|---|
| `OPENCLAW_REGISTRY_URL` | Image registry host. |
| `GITLAB_LOCAL_URL` | Self-hosted GitLab URL the agent should target. |
| `PROXY_URL` | Outbound HTTP proxy if any. |
| `ALIBABA_CLOUD_REGION_ID` | Default Alibaba Cloud region. |
| `ARGOCD_SERVER` | Argo CD API endpoint for the MCP and the deploy workflow. |

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
  `OPENCLAW_VERSION`, and update `newTag:` in
  [`k8s/kustomization.yaml`](k8s/kustomization.yaml) to match.
- New CLI version (gh, glab, terraform, aws, gcloud, aliyun,
  code-server): bump the corresponding entry; the workflow rebuilds
  the image and pushes a fresh tag.

`workflow_dispatch` accepts an optional `git_ref` input to build any
upstream openclaw tag/branch/commit without editing `VERSIONS`.

## License

GPL-3.0. See [`LICENSE`](LICENSE).
