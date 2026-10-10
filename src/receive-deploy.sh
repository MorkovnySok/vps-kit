#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

slug="${1:?missing project slug}"
[[ "$slug" =~ ^[a-z][a-z0-9-]{1,38}$ ]] || exit 1
# Force-command SSH key: the caller may only submit `deploy <40-char commit SHA>`.
if [[ "${SSH_ORIGINAL_COMMAND:-}" =~ ^deploy\ ([0-9a-f]{40})$ ]]; then
  sha="${BASH_REMATCH[1]}"
else
  echo 'Only deploy <commit SHA> is allowed for this SSH key' >&2
  exit 1
fi

config="/etc/vps-kit/projects/${slug}.json"
[[ -r "$config" ]] || { echo "Project is not registered: $slug" >&2; exit 1; }
compose_rel="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["compose"])' "$config")"
base="/opt/apps/${slug}"
mkdir -p "$base/releases"
exec 9>"$base/.deploy.lock"
flock -w 300 9 || { echo 'Deploy lock timeout' >&2; exit 1; }

stage="$(mktemp -d "$base/releases/.incoming-XXXXXXXX")"
trap 'if [[ -n "$stage" && -d "$stage" ]]; then rm -rf -- "$stage"; fi' EXIT
python3 /usr/local/libexec/vps-kit/extract-bundle.py "$stage"
[[ -f "$stage/$compose_rel" ]] || { echo "Missing $compose_rel in deployment bundle" >&2; exit 1; }
[[ -f "$stage/.vpskit.images.yaml" ]] || { echo 'Missing .vpskit.images.yaml' >&2; exit 1; }

# Keep the production .env private and stable across deployments.
mkdir -p "$(dirname "$stage/$compose_rel")"
ln -s "$base/shared/.env" "$stage/.env"
if [[ "$(dirname "$compose_rel")" != '.' ]]; then
  ln -s "$base/shared/.env" "$stage/$(dirname "$compose_rel")/.env"
fi

# A stable project name preserves Docker-managed named volumes and network names.
compose=(docker compose -p "$slug" -f "$stage/$compose_rel" -f "$stage/.vpskit.images.yaml")
"${compose[@]}" config --quiet
# Fail closed: Caddy is the only intended public entry point for Compose apps.
"${compose[@]}" config --format json | python3 -c '
import json,sys
config=json.load(sys.stdin)
for service,definition in config.get("services",{}).items():
    for port in definition.get("ports",[]):
        host=port.get("host_ip") or "0.0.0.0"
        if host not in ("127.0.0.1", "::1"):
            sys.exit(f"Refusing publicly published port on {service}: host={host}; use 127.0.0.1:PORT:PORT")
'
"${compose[@]}" pull

previous=''
[[ ! -L "$base/current" ]] || previous="$(readlink -f "$base/current")"
release="$base/releases/$sha"
if [[ -e "$release" ]]; then
  # Same SHA redeployed, keep prior release content rather than writing over it.
  release="$base/releases/$sha-$(date +%s)"
fi
mv -- "$stage" "$release"
stage=''
compose=(docker compose -p "$slug" -f "$release/$compose_rel" -f "$release/.vpskit.images.yaml")
if ! "${compose[@]}" up -d --no-build --pull never --remove-orphans --wait --wait-timeout 180; then
  echo 'Deployment failed; attempting rollback to previous release...' >&2
  if [[ -n "$previous" && -d "$previous" ]]; then
    docker compose -p "$slug" -f "$previous/$compose_rel" -f "$previous/.vpskit.images.yaml" up -d --no-build --pull never --remove-orphans --wait --wait-timeout 180 || true
  fi
  exit 1
fi
ln -sfn "$release" "$base/.current-new"
mv -Tf "$base/.current-new" "$base/current"
printf 'DEPLOYED project=%s sha=%s release=%s\n' "$slug" "$sha" "$release"
