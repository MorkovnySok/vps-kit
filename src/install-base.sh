#!/usr/bin/env bash
# vps-kit: self-contained VPS bootstrap. Files embedded below by scripts/build.sh.
set -Eeuo pipefail
umask 022

if [[ "$EUID" -ne 0 ]]; then
  echo 'Run as root: sudo bash install.sh --ip YOUR_PUBLIC_IP' >&2
  exit 1
fi

PUBLIC_IP=''
SSH_PORT='22'
while (($#)); do
  case "$1" in
    --ip) PUBLIC_IP="${2:?missing IP}"; shift 2 ;;
    --ssh-port) SSH_PORT="${2:?missing SSH port}"; shift 2 ;;
    -h|--help) echo 'Usage: sudo bash install.sh [--ip PUBLIC_IPV4] [--ssh-port SSH_PORT]'; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

. /etc/os-release
if [[ "${ID:-}" != ubuntu && "${ID:-}" != debian ]]; then
  echo "Only Ubuntu and Debian are supported (detected ${ID:-unknown})." >&2
  exit 1
fi
command -v systemctl >/dev/null || { echo 'Requires systemd VPS (not an unprivileged container).' >&2; exit 1; }

export DEBIAN_FRONTEND=noninteractive
log() { printf '\n\033[1;34m[vps-kit]\033[0m %s\n' "$*"; }
log 'Installing dependencies'
apt-get update -y
apt-get install -y ca-certificates curl gnupg python3 git jq openssh-client openssh-server util-linux apt-transport-https

if [[ -z "$PUBLIC_IP" ]]; then
  PUBLIC_IP="$(curl -4fsSL --max-time 7 https://api.ipify.org || true)"
fi
if [[ -n "$PUBLIC_IP" ]]; then
  python3 - "$PUBLIC_IP" <<'PY'
import ipaddress,sys
try:
    ip = ipaddress.ip_address(sys.argv[1]); assert ip.version == 4 and ip.is_global
except (ValueError, AssertionError):
    sys.exit('Please pass a public IPv4 using --ip')
PY
else
  log 'Public IP not detected. Pass --ip when re-running to use sslip.io domains.'
fi
if ! [[ "$SSH_PORT" =~ ^[0-9]+$ ]] || ((SSH_PORT < 1 || SSH_PORT > 65535)); then
  echo 'Invalid --ssh-port' >&2; exit 1
fi

if ! command -v docker >/dev/null || ! docker compose version >/dev/null 2>&1; then
  log 'Setting up official Docker APT repository'
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL "https://download.docker.com/linux/$ID/gpg" -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  cat > /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/$ID
Suites: ${UBUNTU_CODENAME:-$VERSION_CODENAME}
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
  apt-get update -y
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
else
  log 'Docker Engine and Compose already installed; leaving existing packages intact'
fi
systemctl enable --now docker

if ! command -v caddy >/dev/null; then
  log 'Installing Caddy from the official stable repository'
  apt-get install -y debian-keyring debian-archive-keyring
  curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/gpg.key | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt -o /etc/apt/sources.list.d/caddy-stable.list
  chmod a+r /usr/share/keyrings/caddy-stable-archive-keyring.gpg /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -y
  apt-get install -y caddy
else
  log 'Caddy already installed'
fi

if ! command -v gh >/dev/null; then
  log 'Installing GitHub CLI for optional GitHub secret provisioning'
  apt-get install -y gh
fi

if ! id deploy >/dev/null 2>&1; then
  log 'Creating deploy user'
  useradd --create-home --shell /bin/bash deploy
fi
usermod -aG docker deploy
install -d -m 700 -o deploy -g deploy /home/deploy/.ssh
if [[ ! -f /home/deploy/.ssh/authorized_keys ]]; then
  install -m 600 -o deploy -g deploy /dev/null /home/deploy/.ssh/authorized_keys
fi
install -d -m 755 /opt/apps /etc/vps-kit/projects /etc/caddy/vps-kit /usr/local/libexec/vps-kit
install -d -m 700 /root/.local/share/vps-kit/pending-keys

log 'Writing server configuration'
python3 - "$PUBLIC_IP" "$SSH_PORT" <<'PY'
import json, sys
from pathlib import Path
p = Path('/etc/vps-kit/config.json')
config = json.loads(p.read_text()) if p.exists() else {}
if sys.argv[1]: config['public_ip'] = sys.argv[1]
config['ssh_port'] = int(sys.argv[2])
p.write_text(json.dumps(config, indent=2) + '\n')
p.chmod(0o644)
PY

log 'Ensuring Caddy imports managed project sites without replacing existing config'
CADDYFILE=/etc/caddy/Caddyfile
IMPORT_LINE='import /etc/caddy/vps-kit/*.caddy'
if [[ ! -f "$CADDYFILE" ]]; then
  printf '# Managed sites are included below\n%s\n' "$IMPORT_LINE" > "$CADDYFILE"
elif ! grep -Fxq "$IMPORT_LINE" "$CADDYFILE"; then
  cp -p "$CADDYFILE" "$CADDYFILE.vps-kit-backup.$(date +%Y%m%d-%H%M%S)"
  printf '\n# vps-kit managed sites\n%s\n' "$IMPORT_LINE" >> "$CADDYFILE"
fi
caddy validate --config "$CADDYFILE"
systemctl enable --now caddy
systemctl reload caddy

log 'Installing CLI and restricted SSH deployment receiver'
# BEGIN GENERATED BINARIES: scripts/build.sh inserts transparent source below.
