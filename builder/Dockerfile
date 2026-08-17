# Custom openclaw image for claw-code coding agent.
# Based on the k8s-openclaw builder/Dockerfile but WITHOUT:
#   - JHora / PyJHora / pyswisseph (astrology)
#   - Gmail MCP
#   - Ollama / Whisper / Kokoro TTS / OpenWebUI
#   - Second instance (olga)
#   - gdrive / knowledge corpus
#
# Differences from k8s-openclaw builder:
#   - No Python astrology packages
#   - No ollama / whisper / kokoro
#   - No open-webui
#   - No gdrive / knowledge mounts
#   - Only Mistral Large default (optional MiniMax via secrets)
#
# Args come from .github/workflows/build.yml build step.
ARG BASE_IMAGE
FROM ${BASE_IMAGE}

# Strip the bundled skills the agent could otherwise load at runtime.
#
# ALLOWLIST, NOT DENYLIST. The previous form named the two skills known to be
# dangerous (mcporter, skill-creator) and kept everything else. That is the
# wrong way round: an upstream release that adds a fifty-first skill ships it
# to the agent silently, and nobody finds out until it uses one. The list of
# what may survive lives in BUNDLED_SKILLS_ALLOWED — currently empty, because
# the bot's own skills arrive as reviewed ConfigMap mounts under the workspace
# and nothing upstream ships needs to be kept.
#
# THE ASSERTION IS THE POINT. `rm -rf` on a path upstream has moved exits 0
# and removes nothing. That failure is silent and total: every bundled skill
# ships and the build stays green. So after removing, this enumerates what is
# actually loadable in the image and fails the build on anything the allowlist
# does not name. The SKILLS-LOCKED-ASSERT sentinel is what CI greps for, so
# deleting this block is itself a CI failure rather than a quiet loss.
USER root
COPY BUNDLED_SKILLS_ALLOWED /usr/local/share/BUNDLED_SKILLS_ALLOWED
RUN set -eu; \
    sed 's/#.*//' /usr/local/share/BUNDLED_SKILLS_ALLOWED | awk 'NF' | sort -u > /tmp/allowed; \
    for d in /app/skills/*/; do \
      [ -d "$d" ] || continue; \
      name="$(basename "$d")"; \
      if grep -qxF "$name" /tmp/allowed; then \
        echo "keep   $name"; \
      else \
        echo "strip  $name"; \
        rm -rf "$d"; \
      fi; \
    done; \
    echo "SKILLS-LOCKED-ASSERT: verifying nothing unlisted survived"; \
    find /app -name SKILL.md 2>/dev/null \
      | sed -E 's#.*/([^/]+)/SKILL\.md$#\1#' | sort -u > /tmp/remaining || true; \
    if [ -s /tmp/remaining ] && grep -vxF -f /tmp/allowed /tmp/remaining > /tmp/extra; then \
      echo "FATAL: bundled skills survived the strip that the allowlist does not name:" >&2; \
      cat /tmp/extra >&2; \
      exit 1; \
    fi; \
    rm -f /tmp/allowed /tmp/remaining /tmp/extra; \
    echo "SKILLS-LOCKED-ASSERT: ok"

# gh CLI + GitHub MCP server, installed for autonomous git/github work.
# Image is arm64-only (AKS uses ARM); upstream release archives use `arm64`.
ARG GH_CLI_VERSION
ARG GITHUB_MCP_SERVER_VERSION
USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates jq \
 && curl -fsSL -o /tmp/gh.tar.gz \
      "https://github.com/cli/cli/releases/download/v${GH_CLI_VERSION}/gh_${GH_CLI_VERSION}_linux_arm64.tar.gz" \
 && tar -xzf /tmp/gh.tar.gz -C /tmp \
 && mv "/tmp/gh_${GH_CLI_VERSION}_linux_arm64/bin/gh" /usr/local/bin/gh \
 && rm -rf /tmp/gh.tar.gz "/tmp/gh_${GH_CLI_VERSION}_linux_arm64" \
 && curl -fsSL -o /tmp/github-mcp.tar.gz \
      "https://github.com/github/github-mcp-server/releases/download/v${GITHUB_MCP_SERVER_VERSION}/github-mcp-server_Linux_arm64.tar.gz" \
 && tar -xzf /tmp/github-mcp.tar.gz -C /tmp \
 && mv /tmp/github-mcp-server /usr/local/bin/github-mcp-server \
 && rm -rf /tmp/github-mcp.tar.gz \
 && apt-get purge -y --auto-remove curl \
 && rm -rf /var/lib/apt/lists/* \
 && /usr/local/bin/gh --version

# Baked-in .gitconfig: credential helper reads $GITHUB_TOKEN at git-runtime
RUN install -d -o 1000 -g 1000 /home/node \
 && cat > /home/node/.gitconfig <<'GITCONFIG'
[credential "https://github.com"]
	helper = "!f() { echo username=x-access-token; echo password=$GITHUB_TOKEN; }; f"
GITCONFIG
RUN chown 1000:1000 /home/node/.gitconfig

# Entra / Azure auth tooling
USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl apt-transport-https lsb-release gnupg \
 && mkdir -p /etc/apt/keyrings \
 && curl -sLS https://packages.microsoft.com/keys/microsoft.asc \
      | gpg --dearmor -o /etc/apt/keyrings/microsoft.gpg \
 && AZ_REPO="$(lsb_release -cs)" \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/microsoft.gpg] https://packages.microsoft.com/repos/azure-cli/ $AZ_REPO main" \
      > /etc/apt/sources.list.d/azure-cli.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends azure-cli python3-pyotp \
 && apt-get purge -y --auto-remove apt-transport-https lsb-release gnupg \
 && rm -rf /var/lib/apt/lists/* \
 && /usr/bin/az --version | head -3

# MongoDB driver for the planning store (builder/planning_store.py).
#
# The ONLY pip package in this image, and it is here rather than vendored
# because the alternative is speaking the MongoDB wire protocol by hand.
#
# --break-system-packages: the base is Debian and its python3 is
# externally-managed (PEP 668), so pip refuses to install into it without
# this. That is the correct override here — the image IS the environment,
# there is no venv for a bash runner to activate, and every module is imported
# by bare name from /usr/local/bin.
#
# planning_store imports it LAZILY, inside the functions that reach the
# server. If this layer ever fails or is removed, planning documents still
# spool to the workspace volume and flush once a driver exists; nothing that
# solves an issue breaks.
#
# The default mirrors PYMONGO_VERSION in the root VERSIONS file and must be
# kept equal to it. It exists so the image still builds when the build is
# invoked without --build-arg: an empty ARG here would expand to `pymongo==`,
# which pip rejects, and the whole image would fail on a missing variable
# rather than on anything to do with the driver.
ARG PYMONGO_VERSION=4.17.0
USER root
RUN pip install --no-cache-dir --break-system-packages "pymongo==${PYMONGO_VERSION}" \
 && python3 -c "import pymongo; print('pymongo', pymongo.__version__)"

# k8s-mcp + azure-mcp
COPY --chown=1000:1000 k8s-mcp /opt/k8s-mcp
COPY --chown=1000:1000 azure-mcp /opt/azure-mcp
RUN cd /opt/k8s-mcp \
 && npm install --omit=dev --no-audit --no-fund --loglevel=error \
 && chmod +x k8s-mcp.mjs \
 && cd /opt/azure-mcp \
 && npm install --omit=dev --no-audit --no-fund --loglevel=error \
 && chmod +x azure-mcp.mjs

# AWS CLI v2 + gcloud + aliyun
ARG AWS_CLI_VERSION
ARG GCLOUD_VERSION
ARG ALIYUN_CLI_VERSION
USER root
RUN ARCH=$(dpkg --print-architecture) \
 && AWS_ARCH=$([ "$ARCH" = "arm64" ] && echo "aarch64" || echo "x86_64") \
 && GCP_ARCH=$([ "$ARCH" = "arm64" ] && echo "arm" || echo "x86_64") \
 && apt-get update \
 && apt-get install -y --no-install-recommends unzip \
 && curl -fsSL -o /tmp/awscliv2.zip \
      "https://awscli.amazonaws.com/awscli-exe-linux-${AWS_ARCH}-${AWS_CLI_VERSION}.zip" \
 && unzip -q /tmp/awscliv2.zip -d /tmp \
 && /tmp/aws/install --update \
 && rm -rf /tmp/awscliv2.zip /tmp/aws \
 && curl -fsSL -o /tmp/gcloud.tgz \
      "https://dl.google.com/dl/cloudsdk/channels/rapid/downloads/google-cloud-cli-${GCLOUD_VERSION}-linux-${GCP_ARCH}.tar.gz" \
 && tar -xzf /tmp/gcloud.tgz -C /opt \
 && rm /tmp/gcloud.tgz \
 && /opt/google-cloud-sdk/install.sh --quiet --usage-reporting=false --command-completion=false --path-update=false >/dev/null \
 && ln -sf /opt/google-cloud-sdk/bin/gcloud /usr/local/bin/gcloud \
 && ln -sf /opt/google-cloud-sdk/bin/gsutil /usr/local/bin/gsutil \
 && curl -fsSL -o /tmp/aliyun.tgz \
      "https://github.com/aliyun/aliyun-cli/releases/download/v${ALIYUN_CLI_VERSION}/aliyun-cli-linux-${ALIYUN_CLI_VERSION}-${ARCH}.tgz" \
 && tar -xzf /tmp/aliyun.tgz -C /usr/local/bin aliyun \
 && chmod 0755 /usr/local/bin/aliyun \
 && rm /tmp/aliyun.tgz \
 && apt-get purge -y --auto-remove unzip \
 && rm -rf /var/lib/apt/lists/* \
 && /usr/local/bin/aws --version | head -1 \
 && /usr/local/bin/gcloud --version | head -1 \
 && /usr/local/bin/aliyun --version | head -1

# aws-mcp + gcp-mcp + alicloud-mcp
COPY --chown=1000:1000 aws-mcp /opt/aws-mcp
COPY --chown=1000:1000 gcp-mcp /opt/gcp-mcp
COPY --chown=1000:1000 alicloud-mcp /opt/alicloud-mcp
RUN cd /opt/aws-mcp \
 && npm install --omit=dev --no-audit --no-fund --loglevel=error \
 && chmod +x aws-mcp.mjs \
 && cd /opt/gcp-mcp \
 && npm install --omit=dev --no-audit --no-fund --loglevel=error \
 && chmod +x gcp-mcp.mjs \
 && cd /opt/alicloud-mcp \
 && npm install --omit=dev --no-audit --no-fund --loglevel=error \
 && chmod +x alicloud-mcp.mjs

# Terraform CLI + terraform-mcp-server
ARG TERRAFORM_VERSION
ARG TERRAFORM_MCP_VERSION
USER root
RUN ARCH=$(dpkg --print-architecture) \
 && apt-get update \
 && apt-get install -y --no-install-recommends unzip \
 && curl -fsSL -o /tmp/terraform.zip \
      "https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_linux_${ARCH}.zip" \
 && unzip -q /tmp/terraform.zip -d /usr/local/bin \
 && chmod 0755 /usr/local/bin/terraform \
 && rm /tmp/terraform.zip \
 && curl -fsSL -o /tmp/tf-mcp.zip \
      "https://releases.hashicorp.com/terraform-mcp-server/${TERRAFORM_MCP_VERSION}/terraform-mcp-server_${TERRAFORM_MCP_VERSION}_linux_${ARCH}.zip" \
 && unzip -q /tmp/tf-mcp.zip -d /usr/local/bin terraform-mcp-server \
 && chmod 0755 /usr/local/bin/terraform-mcp-server \
 && rm /tmp/tf-mcp.zip \
 && apt-get purge -y --auto-remove unzip \
 && rm -rf /var/lib/apt/lists/* \
 && terraform version | head -1 \
 && terraform-mcp-server --version 2>&1 | head -1 || true

# Chromium. libnss3-tools provides `certutil`, used at pod start to trust the
# internal CA in Chromium's NSS store (see render-config.sh) so the tester can
# open internal HTTPS deploy URLs without ERR_CERT_AUTHORITY_INVALID.
USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      chromium fonts-liberation \
      libnss3 libnss3-tools libatk-bridge2.0-0 libdrm2 libxkbcommon0 \
      libgbm1 libasound2 libxshmfence1 \
 && rm -rf /var/lib/apt/lists/* \
 && chromium --version

# kubectl + podman
USER root
RUN ARCH=$(dpkg --print-architecture) \
 && KUBECTL_VERSION="$(curl -sL https://dl.k8s.io/release/stable.txt)" \
 && curl -fsSL -o /usr/local/bin/kubectl \
      "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${ARCH}/kubectl" \
 && chmod 0755 /usr/local/bin/kubectl \
 && /usr/local/bin/kubectl version --client=true --output=yaml | head -3 \
 && apt-get update \
 && apt-get install -y --no-install-recommends \
      podman buildah fuse-overlayfs uidmap slirp4netns \
 && rm -rf /var/lib/apt/lists/* \
 && podman --version
RUN install -d -o 1000 -g 1000 /home/node/.config/containers \
 && cat > /home/node/.config/containers/storage.conf <<'STORAGE'
[storage]
driver = "vfs"
runroot = "/home/node/.local/share/containers/run"
graphroot = "/home/node/.local/share/containers/storage"
STORAGE
RUN cat > /home/node/.config/containers/containers.conf <<'CONTAINERS'
[engine]
cgroup_manager = "cgroupfs"
events_logger = "file"
runtime = "crun"
CONTAINERS
RUN chown -R 1000:1000 /home/node/.config

# code-server
ARG CODE_SERVER_VERSION
USER root
RUN ARCH=$(dpkg --print-architecture) \
 && curl -fsSL -o /tmp/code-server.deb \
      "https://github.com/coder/code-server/releases/download/v${CODE_SERVER_VERSION}/code-server_${CODE_SERVER_VERSION}_${ARCH}.deb" \
 && apt-get update \
 && apt-get install -y --no-install-recommends /tmp/code-server.deb \
 && rm /tmp/code-server.deb \
 && rm -rf /var/lib/apt/lists/* \
 && install -d -o 1000 -g 1000 /home/node/.config/code-server \
 && install -d -o 1000 -g 1000 /home/node/.local/share/code-server \
 && code-server --version | head -1

# debug-mcp
COPY debug-mcp /opt/debug-mcp
RUN chown -R 1000:1000 /opt/debug-mcp \
 && cd /opt/debug-mcp \
 && su node -c "cd /opt/debug-mcp && npm install --omit=dev --no-audit --no-fund --loglevel=error" \
 && chmod +x /opt/debug-mcp/debug-mcp.mjs

# Issue-watcher scripts invoked by the issue-watcher CronJob.
#   heartbeat-issue-tick — read-only planner: emits JSON of repos +
#                          assigned issues + active Jobs per repo.
#   cron-issue-spawn     — runs the planner and `kubectl apply`s a
#                          Job per toSpawn entry (one Job = one
#                          `openclaw agent --local` invocation that
#                          fixes the issue end-to-end).
USER root
COPY --chmod=0755 heartbeat-issue-tick.py /usr/local/bin/heartbeat-issue-tick
COPY --chmod=0755 cron-issue-spawn.sh    /usr/local/bin/cron-issue-spawn
COPY --chmod=0755 fixer-runner.sh        /usr/local/bin/fixer-runner
# Tester subsystem — parallel to the issue-fixer. Same pattern: a
# CronJob runs cron-tester-spawn, which calls tester-tick to list
# repos with new default-branch HEAD, then kubectl-exec's a
# tester-runner subprocess inside the openclaw pod. See
# k8s/051-tester.yaml.
COPY --chmod=0755 tester-tick.py                /usr/local/bin/tester-tick
COPY --chmod=0755 cron-tester-spawn.sh          /usr/local/bin/cron-tester-spawn
COPY --chmod=0755 tester-runner.sh              /usr/local/bin/tester-runner
COPY --chmod=0755 tester-upload-screenshots.py  /usr/local/bin/tester-upload-screenshots

# Reviewer subsystem — the third planner/runner pair, same shape again. See
# k8s/052-reviewer.yaml.
COPY --chmod=0755 reviewer-tick.py         /usr/local/bin/reviewer-tick
COPY --chmod=0755 cron-reviewer-spawn.sh   /usr/local/bin/cron-reviewer-spawn
COPY --chmod=0755 reviewer-runner.sh       /usr/local/bin/reviewer-runner

# Estimation — sizes a story on the cheap planning model one tick before the
# solver picks it up, so the solver can route by size.
COPY --chmod=0755 estimate-runner.sh       /usr/local/bin/estimate-runner

# Shared Python modules. Installed FLAT and extension-less alongside the
# scripts because that is how they are imported at runtime — by bare name,
# from /usr/local/bin, which is on sys.path for a script living there. The
# test suite imports them the same way so it tests this arrangement and not
# another one.
#
# A module that is merged but not listed here is deployed and then absent:
# the code that imports it raises ImportError on every tick, in a CronJob log
# nobody reads. `test_shipped_modules` exists to fail the build instead.
COPY --chmod=0644 issue_status.py       /usr/local/bin/issue_status.py
COPY --chmod=0644 issue_priority.py     /usr/local/bin/issue_priority.py
COPY --chmod=0644 story_points.py       /usr/local/bin/story_points.py
COPY --chmod=0644 story_estimate.py     /usr/local/bin/story_estimate.py
COPY --chmod=0644 lexical_guard.py      /usr/local/bin/lexical_guard.py
COPY --chmod=0644 review_subject.py     /usr/local/bin/review_subject.py
COPY --chmod=0644 security_reports.py   /usr/local/bin/security_reports.py
COPY --chmod=0644 queue_state.py        /usr/local/bin/queue_state.py
COPY --chmod=0644 delivering_pr.py      /usr/local/bin/delivering_pr.py
COPY --chmod=0644 project_allowlist.py  /usr/local/bin/project_allowlist.py
COPY --chmod=0644 planning_docs.py      /usr/local/bin/planning_docs.py
COPY --chmod=0644 planning_store.py     /usr/local/bin/planning_store.py
COPY --chmod=0644 sprint_current.py     /usr/local/bin/sprint_current.py
COPY --chmod=0644 sprint_schedule.py    /usr/local/bin/sprint_schedule.py

# Runtime controls. The `.sh` halves are SOURCED by the runners and must sit
# next to the CLI that shares their name — agent-models sources
# agent-models.sh from its own directory, exactly as the tests arrange it.
COPY --chmod=0755 agent-limits          /usr/local/bin/agent-limits
COPY --chmod=0644 agent-limits.sh       /usr/local/bin/agent-limits.sh
COPY --chmod=0755 agent-models          /usr/local/bin/agent-models
COPY --chmod=0644 agent-models.sh       /usr/local/bin/agent-models.sh
COPY --chmod=0755 agent-thinking        /usr/local/bin/agent-thinking
COPY --chmod=0644 agent-thinking.sh     /usr/local/bin/agent-thinking.sh
COPY --chmod=0644 agent-slot.sh         /usr/local/bin/agent-slot.sh
COPY --chmod=0755 security-level        /usr/local/bin/security-level
COPY --chmod=0755 llm-quota             /usr/local/bin/llm-quota

# Permission boundary. Nothing the bot does autonomously touches a repository
# outside this list.
COPY --chmod=0755 project-allow          /usr/local/bin/project-allow
COPY --chmod=0644 project-kind.sh        /usr/local/bin/project-kind.sh
COPY --chmod=0644 project-instructions.sh /usr/local/bin/project-instructions.sh

# Planning / reporting surface.
COPY --chmod=0755 planning              /usr/local/bin/planning
COPY --chmod=0755 planning-record       /usr/local/bin/planning-record
COPY --chmod=0755 record-deliveries     /usr/local/bin/record-deliveries

# Notification and rendering helpers.
COPY --chmod=0755 telegram-notify       /usr/local/bin/telegram-notify
COPY --chmod=0755 mermaid-render        /usr/local/bin/mermaid-render
COPY --chmod=0644 mermaid-theme.json    /usr/local/share/mermaid-theme.json

# The skills lockdown, run as root by the fix-perms init container.
COPY --chmod=0755 lock-skill-dirs.sh    /usr/local/bin/lock-skill-dirs

# Timezone: Europe/Zurich. Without this, libc time + Node Date default
# to UTC, so chat timestamps, heartbeat log lines, and the runtime
# context the LLM sees are off by 1-2h vs the user's locale. The
# `Europe/Zurich` IANA name handles CET ↔ CEST automatically.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata \
 && ln -snf /usr/share/zoneinfo/Europe/Zurich /etc/localtime \
 && echo "Europe/Zurich" > /etc/timezone \
 && rm -rf /var/lib/apt/lists/*
ENV TZ=Europe/Zurich

USER node