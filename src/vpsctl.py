#!/usr/bin/env python3
"""vps-kit: project provisioning for GitHub-hosted CI + SSH deploy."""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys

CONFIG = Path(os.getenv('VPSKIT_CONFIG', '/etc/vps-kit/config.json'))
PROJECT_DIR = Path(os.getenv('VPSKIT_PROJECTS', '/etc/vps-kit/projects'))
APPS_DIR = Path(os.getenv('VPSKIT_APPS', '/opt/apps'))
CADDY_DIR = Path(os.getenv('VPSKIT_CADDY_DIR', '/etc/caddy/vps-kit'))
AUTH_KEYS = Path(os.getenv('VPSKIT_AUTH_KEYS', '/home/deploy/.ssh/authorized_keys'))
KEY_DIR = Path(os.getenv('VPSKIT_KEY_DIR', '/root/.local/share/vps-kit/pending-keys'))
RECEIVER = os.getenv('VPSKIT_RECEIVER', '/usr/local/libexec/vps-kit/receive-deploy')
HOST_KEY = Path(os.getenv('VPSKIT_HOST_KEY', '/etc/ssh/ssh_host_ed25519_key.pub'))
VALID_SLUG = re.compile(r'^[a-z][a-z0-9-]{1,38}$')
VALID_REPO = re.compile(r'^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$')
VALID_PATH = re.compile(r'^(?!/)(?!.*(?:^|/)\.\.(?:/|$))[a-zA-Z0-9_./-]+$')
VALID_BRANCH = re.compile(r'^[A-Za-z0-9._/-]+$')
VALID_HOST = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9.-]{0,251}[a-zA-Z0-9]$')


def error(msg):
    raise SystemExit(f'ERROR: {msg}')


def run(*args, input=None, check=True):
    return subprocess.run(args, check=check, input=input, text=True)


def needs_root():
    if os.geteuid() != 0 and os.getenv('VPSKIT_ALLOW_NONROOT') != '1':
        error('run with sudo')


def read_project(slug):
    f = PROJECT_DIR / f'{slug}.json'
    if not f.exists():
        error(f'project {slug!r} not found')
    return json.loads(f.read_text())


def write_secret(p, data):
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w') as out:
        out.write(data)


def project_add(a):
    needs_root()
    slug, repo = a.slug, a.repo
    if not VALID_SLUG.fullmatch(slug):
        error('slug must be 2–39 lowercase letters, numbers or hyphens, starting with a letter')
    if not VALID_REPO.fullmatch(repo) or '..' in repo:
        error('repo must be OWNER/REPO')
    if not VALID_PATH.fullmatch(a.compose) or a.compose.startswith('./.'):
        error('compose path must be a safe relative path such as compose.yaml')
    if not VALID_BRANCH.fullmatch(a.branch) or '..' in a.branch:
        error('invalid branch')
    if not 1024 <= a.port <= 65535:
        error('port should be between 1024 and 65535')
    if (PROJECT_DIR / f'{slug}.json').exists():
        error(f'project {slug} already exists (no keys or files overwritten)')
    for f in PROJECT_DIR.glob('*.json'):
        if json.loads(f.read_text()).get('port') == a.port:
            error(f'port {a.port} already used by {f.stem}')
    settings = json.loads(CONFIG.read_text())
    if a.domain:
        domain = a.domain.lower()
        if not VALID_HOST.fullmatch(domain) or '..' in domain:
            error('invalid domain')
    else:
        ip = settings.get('public_ip')
        if not ip:
            error('no public IP configured; pass --domain or rerun install with --ip')
        domain = f'{slug}.{ip.replace(".", "-")}.sslip.io'
    for f in PROJECT_DIR.glob('*.json'):
        if json.loads(f.read_text()).get('domain') == domain:
            error(f'domain already managed: {domain}')
    # Guard against overriding an existing site in /etc/caddy/Caddyfile.
    caddyfile = Path('/etc/caddy/Caddyfile')
    if not a.no_caddy and not os.getenv('VPSKIT_CADDY_DIR') and caddyfile.exists() and domain in caddyfile.read_text():
        error(f'domain {domain} already appears in Caddyfile; migrate existing site manually')
    KEY_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(KEY_DIR, 0o700)
    key = KEY_DIR / f'{slug}.key'
    if key.exists():
        error(f'pending SSH key exists at {key} — inspect before retrying')
    run('ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C', f'vps-kit-{slug}', '-f', str(key))
    os.chmod(key, 0o600)
    pub = key.with_suffix('.key.pub').read_text().strip()
    AUTH_KEYS.parent.mkdir(parents=True, exist_ok=True)
    # OpenSSH restrict disallows port/agent/X forwarding and PTY.
    with AUTH_KEYS.open('a') as f:
        f.write(f'command="{RECEIVER} {slug}",restrict {pub}\n')
    os.chmod(AUTH_KEYS.parent, 0o700)
    os.chmod(AUTH_KEYS, 0o600)
    if os.getenv('VPSKIT_ALLOW_NONROOT') != '1':
        run('chown', '-R', 'deploy:deploy', str(AUTH_KEYS.parent))
    base = APPS_DIR / slug
    shared = base / 'shared'
    releases = base / 'releases'
    releases.mkdir(parents=True, exist_ok=True)
    shared.mkdir(parents=True, exist_ok=True)
    env = shared / '.env'
    if not env.exists():
        env.touch(mode=0o600)
    os.chmod(env, 0o600)
    if os.getenv('VPSKIT_ALLOW_NONROOT') != '1':
        run('chown', '-R', 'deploy:deploy', str(base))
    PROJECT_DIR.mkdir(parents=True, exist_ok=True)
    project = dict(slug=slug, repo=repo, port=a.port, compose=a.compose, branch=a.branch, domain=domain)
    write_secret(PROJECT_DIR / f'{slug}.json', json.dumps(project, indent=2) + '\n')
    # Project metadata is not secret. Receiver needs read access.
    os.chmod(PROJECT_DIR / f'{slug}.json', 0o644)
    if not a.no_caddy:
        CADDY_DIR.mkdir(parents=True, exist_ok=True)
        site = CADDY_DIR / f'{slug}.caddy'
        site.write_text(f'{domain} {{\n    reverse_proxy 127.0.0.1:{a.port}\n}}\n')
        validation = subprocess.run(['caddy', 'validate', '--config', '/etc/caddy/Caddyfile'], capture_output=True, text=True)
        if validation.returncode != 0:
            site.unlink()
            error(f'Caddy config invalid; site rolled back: {validation.stderr.strip()}')
        run('systemctl', 'reload', 'caddy')
    print(f'\nProject registered: {slug} ({repo})')
    print(f'HTTPS target: https://{domain} (after app deploy and DNS/TLS issuance)')
    print(f'Compose port must be bound to 127.0.0.1:{a.port} on the VPS.')
    print(f'Edit production env on VPS: sudo nano {env}')
    print(f'Next: sudo vpsctl github configure {slug} (requires gh auth login)')
    print(f'Then: vpsctl workflow {slug} > deploy.yml')
    print('Commit deploy.yml to .github/workflows/deploy.yml in the application repository.')


def workflow_text(p):
    slug = p['slug']
    branch = p['branch']
    compose = p['compose']
    # Values are validated at project creation (never shell-sourced).
    return f'''# Generated by vps-kit. GitHub-hosted build -> GHCR -> restricted SSH deploy.
name: Deploy {slug}
on:
  push:
    branches: ['{branch}']
  workflow_dispatch:

permissions:
  contents: read
  packages: write

concurrency:
  group: deploy-{slug}
  cancel-in-progress: false

jobs:
  deploy:
    runs-on: ubuntu-latest
    timeout-minutes: 45
    steps:
      - uses: actions/checkout@v6
      - uses: docker/setup-buildx-action@v3
      - uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{{{ github.actor }}}}
          password: ${{{{ secrets.GITHUB_TOKEN }}}}

      - name: Build and publish Compose services
        env:
          COMPOSE_FILE_PATH: '{compose}'
        shell: bash
        run: |
          set -euo pipefail
          # A placeholder .env avoids missing env_file for build-only CI.
          mkdir -p "$(dirname "$COMPOSE_FILE_PATH")"
          touch "$(dirname "$COMPOSE_FILE_PATH")/.env"
          touch .env
          # Optional non-secret build placeholders can go into .env.ci.
          if [[ -f .env.ci ]]; then set -a; source .env.ci; set +a; fi
          echo 'services: {{}}' > .vpskit.images.yaml
          compose_json=$(docker compose -f "$COMPOSE_FILE_PATH" config --no-interpolate --no-env-resolution --format json)
          mapfile -t build_services < <(printf '%s\\n' "$compose_json" | jq -r '.services | to_entries[] | select(.value.build != null) | .key')
          if [[ ${{#build_services[@]}} -eq 0 ]]; then
            echo 'No build: services in Compose: nothing to publish to GHCR.'
          else
            repo="${{GITHUB_REPOSITORY,,}}"
            echo 'services:' > .vpskit.images.yaml
            for svc in "${{build_services[@]}}"; do
              safe_svc="${{svc,,}}"
              printf '  %s:\\n    image: ghcr.io/%s-%s:sha-%s\\n' "$svc" "$repo" "$safe_svc" "$GITHUB_SHA" >> .vpskit.images.yaml
            done
            docker compose -f "$COMPOSE_FILE_PATH" -f .vpskit.images.yaml build "${{build_services[@]}}"
            docker compose -f "$COMPOSE_FILE_PATH" -f .vpskit.images.yaml push "${{build_services[@]}}"
          fi

      - name: Send deployment to VPS
        env:
          DEPLOY_HOST: ${{{{ secrets.DEPLOY_HOST }}}}
          DEPLOY_PORT: ${{{{ secrets.DEPLOY_PORT }}}}
          DEPLOY_SSH_KEY: ${{{{ secrets.DEPLOY_SSH_KEY }}}}
          DEPLOY_KNOWN_HOSTS: ${{{{ secrets.DEPLOY_KNOWN_HOSTS }}}}
        shell: bash
        run: |
          set -euo pipefail
          test -n "$DEPLOY_HOST" && test -n "$DEPLOY_SSH_KEY" && test -n "$DEPLOY_KNOWN_HOSTS"
          mkdir -p ~/.ssh
          chmod 700 ~/.ssh
          printf '%s\\n' "$DEPLOY_SSH_KEY" > ~/.ssh/vpskit_deploy
          printf '%s\\n' "$DEPLOY_KNOWN_HOSTS" > ~/.ssh/known_hosts
          chmod 600 ~/.ssh/vpskit_deploy ~/.ssh/known_hosts
          tar --exclude='./.git' --exclude='./.env' --exclude='*/.env' \\
              --exclude='./.env.*' --exclude='*/.env.*' --exclude='*/node_modules' \\
              --exclude='*/bin' --exclude='*/obj' -czf "$RUNNER_TEMP/deploy.tgz" .
          ssh -T -i ~/.ssh/vpskit_deploy -p "${{DEPLOY_PORT:-22}}" \\
              -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes \\
              -o ServerAliveInterval=30 -o ServerAliveCountMax=6 \\
              "deploy@$DEPLOY_HOST" "deploy $GITHUB_SHA" < "$RUNNER_TEMP/deploy.tgz"
'''


def project_workflow(a):
    p = read_project(a.slug)
    sys.stdout.write(workflow_text(p))


def github_configure(a):
    needs_root()
    p = read_project(a.slug)
    key = KEY_DIR / f'{a.slug}.key'
    if not key.exists():
        error(f'pending key not found: {key}. It may already have been uploaded.')
    if shutil.which('gh') is None:
        error('gh CLI is not installed')
    if subprocess.run(['gh', 'auth', 'status'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
        error('GitHub CLI not logged in for current OS user. Run: sudo gh auth login --web')
    cfg = json.loads(CONFIG.read_text())
    host = cfg.get('public_ip')
    if not host:
        error('missing public_ip in vps-kit config')
    if not HOST_KEY.exists():
        error('server ED25519 SSH host public key not found; check sshd installation')
    host_key = HOST_KEY.read_text().strip().split()
    if len(host_key) < 2 or host_key[0] != 'ssh-ed25519':
        error('invalid SSH host key')
    known_hosts = f'{host} {host_key[0]} {host_key[1]}\n'
    secrets = {
        'DEPLOY_HOST': host + '\n',
        'DEPLOY_PORT': f"{cfg.get('ssh_port', 22)}\n",
        'DEPLOY_KNOWN_HOSTS': known_hosts,
        'DEPLOY_SSH_KEY': key.read_text(),
    }
    for name, value in secrets.items():
        print(f'Uploading secret {name} to {p["repo"]} ...')
        run('gh', 'secret', 'set', name, '--repo', p['repo'], input=value)
    # Key remains only in GitHub Actions secrets and authorized_keys on the VPS.
    key.unlink()
    pub = key.with_suffix('.key.pub')
    if pub.exists(): pub.unlink()
    print('Secrets set. Temporary private key removed from VPS.')
    print(f'Generate your workflow: vpsctl workflow {a.slug} > deploy.yml')
    print('Note: private GHCR packages also need a read:packages token on VPS (vpsctl registry login).')


def registry_login(a):
    needs_root()
    print('GitHub personal access token (classic), scope read:packages, will be required.')
    print('Docker stores credentials for the deploy user on this VPS. Use a dedicated low-privilege token.')
    run('runuser', '-u', 'deploy', '--', 'docker', 'login', 'ghcr.io', '-u', a.user)


def show_status(_):
    for f in sorted(PROJECT_DIR.glob('*.json')):
        p = json.loads(f.read_text())
        print(f'{p["slug"]:20} {p["repo"]:35} {p["domain"]}  -> 127.0.0.1:{p["port"]}')
    if shutil.which('docker'):
        subprocess.run(['docker', 'ps', '--format', 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'], check=False)


def doctor(_):
    for name, cmd in [('Docker', ['docker', 'info', '--format', '{{.ServerVersion}}']),
                      ('Compose', ['docker', 'compose', 'version']),
                      ('Caddy', ['systemctl', 'is-active', 'caddy'])]:
        print(f'[{name}]')
        subprocess.run(cmd, check=False)
    if shutil.which('curl'):
        print('[GHCR connectivity; HTTP 401 is expected when reachable]')
        subprocess.run(['curl', '-ILsS', '--max-time', '10', '-o', '/dev/null', '-w', 'HTTP %{http_code}\\n', 'https://ghcr.io/v2/'], check=False)


def main():
    ap = argparse.ArgumentParser(prog='vpsctl', description='Manage Compose projects on a VPS; GitHub-hosted build, SSH deploy.')
    sub = ap.add_subparsers(dest='command', required=True)
    proj = sub.add_parser('project')
    ps = proj.add_subparsers(dest='action', required=True)
    add = ps.add_parser('add')
    add.add_argument('slug'); add.add_argument('--repo', required=True)
    add.add_argument('--port', type=int, required=True); add.add_argument('--compose', default='compose.yaml')
    add.add_argument('--branch', default='main'); add.add_argument('--domain'); add.add_argument('--no-caddy', action='store_true')
    add.set_defaults(fn=project_add)
    wf = sub.add_parser('workflow'); wf.add_argument('slug'); wf.set_defaults(fn=project_workflow)
    github = sub.add_parser('github')
    gs = github.add_subparsers(dest='action', required=True)
    gc = gs.add_parser('configure'); gc.add_argument('slug'); gc.set_defaults(fn=github_configure)
    registry = sub.add_parser('registry')
    rs = registry.add_subparsers(dest='action', required=True)
    lg = rs.add_parser('login'); lg.add_argument('user'); lg.set_defaults(fn=registry_login)
    st = sub.add_parser('status'); st.set_defaults(fn=show_status)
    doc = sub.add_parser('doctor'); doc.set_defaults(fn=doctor)
    args = ap.parse_args()
    args.fn(args)


if __name__ == '__main__':
    main()
