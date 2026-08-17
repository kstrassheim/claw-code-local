# shellcheck shell=bash
# project-kind: what does a TARGET project actually deploy? Sourced (not
# executed) by the three runners, same pattern as
# /usr/local/bin/project-instructions.
#
#   web        a site to browse           frontend/ or backend/ (or: nothing
#                                         else matched — the safe fallback)
#   automation PowerShell runbooks        runbooks/ or automation*.tf
#   dwh        source DBs + Data Factory  synapse*.tf / datafactory*.tf /
#              + Synapse                  sql_init_*.sql
#   k8s        a workload in its OWN AKS  aks.tf|kubernetes.tf + k8s/
#              cluster
#   aksbot     ...and that workload is    the same, AND the manifests deploy an
#              an openclaw bot            openclaw instance (see below)
#
# A SET, NOT A SINGLE VALUE
# -------------------------
# Real projects combine these: a warehouse with a small web front end, a
# runbook repo that also ships a portal. This used to be a first-match-wins
# if/elif chain in each runner, which silently dropped every part but one — a
# DWH + website was classified `dwh`, the site was never opened, and the
# tester prompt went as far as asserting the project HAD no website while its
# URL sat in the pipeline trace. So detection returns EVERY kind that matches
# and the callers compose one prompt section per kind.
#
# The order is fixed — automation, dwh, k8s|aksbot, web — and callers keep it
# when they compose. It is the order the parts should be TESTED in: the runbook
# and data side first, the site last, because the site usually reads whatever
# the pipelines produced (and is often served from the cluster), so a failure
# there is explained by what came before.
#
# k8s VERSUS aksbot
# -----------------
# These two are mutually exclusive: `aksbot` is `k8s` upgraded. Cluster plus
# manifests gets you `k8s`, whose protocol is generic — connect, then pods,
# deployments, cronjobs, events, restarts. `aksbot` adds the stages that only
# make sense against an openclaw bot: its scheduled tick fired, its last run
# succeeded on its own terms, no stale lock directory, and the effect it was
# supposed to have actually landed in the database.
#
# The upgrade is decided by WHAT IS IN THE MANIFESTS, not by what is absent
# from the repo root. The first version used "no frontend/" as the test, on
# the theory that a repo with a frontend is a website rather than a bot. That
# proxy fails in both directions: a website deployed to AKS got no cluster
# check at all, and a bot repo that also ships a small UI would have had the
# bot stages run as though it were a plain site. Reading the manifests answers
# the actual question — is the thing in this cluster an openclaw instance? —
# and costs one grep (or, for the tester, nothing extra: the k8s kind already
# forces the checkout it then greps).
#
# The two detectors below must agree: the tester reads the repo tree over the
# API — `GET /repos/{owner}/{repo}/contents?ref={sha}`, one root entry per
# line, at the COMMIT UNDER TEST — because it often has no checkout, while the
# reviewer and solver read the checkout they already hold. Same patterns, two
# sources; the tree is passed in, so this file never makes a request of its
# own and stays testable without a token. The API side matches
# case-insensitively and the filesystem side does not — a project spelling it
# `Runbooks/` is detected by the tester and not by the other two, which is the
# pre-existing behaviour of both and not worth diverging further over.

# The set. Callers read it; nothing outside this file should assign it.
PROJECT_KINDS=""

_pk_add() {
  case " $PROJECT_KINDS " in
    *" $1 "*) ;;
    *) PROJECT_KINDS="${PROJECT_KINDS:+$PROJECT_KINDS }$1" ;;
  esac
}

# has_kind <kind> — true when the project is (also) that kind.
has_kind() {
  case " $PROJECT_KINDS " in
    *" $1 "*) return 0 ;;
  esac
  return 1
}

# has_nonweb_kind — true when the project deploys something that is not a site.
# Gates everything that only makes sense against Azure resources: the source
# checkout, the scope rule, the Entra auth rule.
has_nonweb_kind() {
  has_kind automation || has_kind dwh || has_kind k8s || has_kind aksbot
}

# has_cluster_kind — either flavour of "this project owns an AKS cluster".
has_cluster_kind() {
  has_kind k8s || has_kind aksbot
}

# kind_count — how many kinds matched. 1 means "compose exactly what a
# single-kind project used to get"; >1 means the multi-part framing.
kind_count() {
  # shellcheck disable=SC2086
  set -- $PROJECT_KINDS
  echo "$#"
}

# kind_title <kind> — SHOUTED short name, for a part heading.
kind_title() {
  case "$1" in
    automation) echo "AZURE AUTOMATION" ;;
    dwh)        echo "DWH (source DBs + Data Factory + Synapse)" ;;
    k8s)        echo "AKS WORKLOAD" ;;
    aksbot)     echo "AKS WORKLOAD (an openclaw bot)" ;;
    web)        echo "WEB APPLICATION" ;;
    *)          echo "$1" ;;
  esac
}

# kind_label <kind> — prose name, for a sentence describing the project.
kind_label() {
  case "$1" in
    automation) echo "Azure Automation runbooks" ;;
    dwh)        echo "a data warehouse (source databases, Data Factory pipelines, a Synapse SQL pool)" ;;
    k8s)        echo "a workload running in the project's own AKS cluster" ;;
    aksbot)     echo "an openclaw bot running in the project's own AKS cluster" ;;
    web)        echo "a web application with a site to browse" ;;
    *)          echo "$1" ;;
  esac
}

# kinds_english — "a data warehouse (...) AND a web application with a site to
# browse". Used in the one sentence that tells the agent what it is looking at.
kinds_english() {
  _pk_out=""; _pk_n=0
  for _pk_k in $PROJECT_KINDS; do _pk_n=$((_pk_n + 1)); done
  _pk_i=0
  for _pk_k in $PROJECT_KINDS; do
    _pk_i=$((_pk_i + 1))
    _pk_this="$(kind_label "$_pk_k")"
    if [ "$_pk_i" = 1 ]; then
      _pk_out="$_pk_this"
    elif [ "$_pk_i" = "$_pk_n" ]; then
      _pk_out="$_pk_out AND $_pk_this"
    else
      _pk_out="$_pk_out, $_pk_this"
    fi
  done
  echo "$_pk_out"
}

# ---------------------------------------------------------------------------
# Detection — from a repo tree listing (one root entry per line)
# ---------------------------------------------------------------------------
_pk_tree_has() { printf '%s\n' "$_PK_TREE" | grep -qiE "$1"; }

# A root listing cannot say whether the workload is a bot — that is in the
# manifests. So this always yields the generic `k8s`; the caller upgrades it
# with refine_aks_kind once it has a checkout.
detect_project_kinds_from_tree() {
  _PK_TREE="$1"
  PROJECT_KINDS=""
  if _pk_tree_has '^runbooks$|^automation.*\.tf$'; then _pk_add automation; fi
  if _pk_tree_has '^(synapse|datafactory).*\.tf$|^sql_init_.*\.sql$'; then _pk_add dwh; fi
  if _pk_tree_has '^(aks|kubernetes)\.tf$' && _pk_tree_has '^k8s$'; then _pk_add k8s; fi
  if _pk_tree_has '^frontend$|^backend$'; then _pk_add web; fi
  # Nothing recognised — keep the browser test rather than silently testing
  # nothing. An unrecognised project loses no coverage it used to have.
  [ -n "$PROJECT_KINDS" ] || PROJECT_KINDS=web
  return 0
}

# ---------------------------------------------------------------------------
# Detection — from a checkout
# ---------------------------------------------------------------------------
# True when ANY argument exists. The obvious `ls a* b* c* >/dev/null 2>&1` is
# wrong and was the previous implementation in two of the runners: ls exits
# non-zero if ANY operand is missing, so it means "all three patterns match",
# not "any of them does". A project with datafactory.tf and no synapse*.tf was
# therefore never detected as dwh by the reviewer or the solver, while the
# tester (grep over the tree, real OR) said it was.
_pk_any_exists() {
  for _pk_g in "$@"; do
    [ -e "$_pk_g" ] && return 0
  done
  return 1
}

detect_project_kinds_from_dir() {
  _pk_dir="$1"
  PROJECT_KINDS=""
  if [ -d "$_pk_dir/runbooks" ] || _pk_any_exists "$_pk_dir"/automation*.tf; then
    _pk_add automation
  fi
  if _pk_any_exists "$_pk_dir"/synapse*.tf "$_pk_dir"/datafactory*.tf \
                    "$_pk_dir"/sql_init_*.sql; then
    _pk_add dwh
  fi
  if { [ -f "$_pk_dir/aks.tf" ] || [ -f "$_pk_dir/kubernetes.tf" ]; } \
     && [ -d "$_pk_dir/k8s" ]; then
    _pk_add k8s
  fi
  if [ -d "$_pk_dir/frontend" ] || [ -d "$_pk_dir/backend" ]; then
    _pk_add web
  fi
  [ -n "$PROJECT_KINDS" ] || PROJECT_KINDS=web
  # The manifests are right here, so answer the bot question immediately.
  refine_aks_kind "$_pk_dir"
  return 0
}

# ---------------------------------------------------------------------------
# k8s -> aksbot, from the manifests
# ---------------------------------------------------------------------------
# k8s_workload_is_bot <dir> — does what this repo deploys to its cluster
# include an openclaw instance? The bot ships as a `claw-*` deployment with an
# `openclaw.json` ConfigMap and a PVC-mounted ~/.openclaw, so its name is all
# over its own manifests and appears in nobody else's.
k8s_workload_is_bot() {
  [ -d "$1/k8s" ] || return 1
  grep -rqiE 'openclaw|claw-[a-z0-9]' "$1/k8s" 2>/dev/null
}

# refine_aks_kind <dir> — upgrade a generic k8s kind to aksbot when the
# manifests show a bot. Safe to call when the project has no cluster, when the
# directory does not exist, or twice. A caller with no checkout simply skips
# it and keeps the generic protocol, which is the right degradation: the bot
# stages need the repo anyway to resolve what they are looking at.
refine_aks_kind() {
  has_kind k8s || return 0
  k8s_workload_is_bot "$1" || return 0
  _pk_new=""
  for _pk_k in $PROJECT_KINDS; do
    [ "$_pk_k" = "k8s" ] && _pk_k=aksbot
    _pk_new="${_pk_new:+$_pk_new }$_pk_k"
  done
  PROJECT_KINDS="$_pk_new"
  return 0
}
