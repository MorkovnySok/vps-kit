#!/usr/bin/env python3
"""vps-kit: project provisioning for GitHub-hosted CI + SSH deploy."""
import argparse
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys

sys.path.append(os.getenv('VPSKIT_LIBEXEC', '/usr/local/libexec/vps-kit'))
import vpskit_vpn

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
    raise SystemExit(f'Ошибка: {msg}')


def run(*args, input=None, check=True):
    return subprocess.run(args, check=check, input=input, text=True)


def needs_root():
    if os.geteuid() != 0 and os.getenv('VPSKIT_ALLOW_NONROOT') != '1':
        error('для этой команды нужны права root. Повтори её с sudo.')


def load_json(path):
    if not path.exists():
        error(f'не найден {path}. Сначала установи vps-kit: sudo bash install.sh --ip PUBLIC_IPV4')
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        error(f'не удалось прочитать JSON в {path}. Проверь файл перед повторным запуском.')


def projects():
    return [load_json(f) for f in sorted(PROJECT_DIR.glob('*.json'))]


def read_project(slug):
    if not VALID_SLUG.fullmatch(slug):
        error('некорректное имя проекта. Посмотри имена: vpsctl status')
    f = PROJECT_DIR / f'{slug}.json'
    if not f.exists():
        error(f'проект {slug!r} не найден. Посмотри имена: vpsctl status')
    return load_json(f)


class WizardCancelled(Exception):
    pass


def is_interactive():
    return sys.stdin.isatty() and sys.stdout.isatty()


def require_terminal():
    if not is_interactive():
        error('мастеру нужен интерактивный терминал. Подключись по SSH или используй параметры из vpsctl project add --help.')


def choose(title, options, default=0):
    """Small SSH-friendly menu, with a plain numbered fallback for dumb terminals."""
    print(f'\n{title}')
    terminal = shutil.get_terminal_size()
    if (not is_interactive() or os.name != 'posix' or os.getenv('TERM', '') in ('', 'dumb')
            or terminal.lines < len(options) + 4 or terminal.columns < 60):
        for i, label in enumerate(options, 1):
            print(f'  {i}. {label}')
        while True:
            value = input(f'Номер [Enter = {default + 1}]: ').strip()
            if not value:
                return default
            if value.isascii() and value.isdigit() and 1 <= int(value) <= len(options):
                return int(value) - 1
            print(f'Введи число от 1 до {len(options)}.')

    import select
    import termios
    import tty

    print('↑/↓ — выбор · 1–9 — быстрый выбор · Enter — продолжить · Esc — отмена')
    width = shutil.get_terminal_size().columns
    selected = default

    def draw():
        for i, label in enumerate(options):
            line = f' {">" if i == selected else " "} {i + 1}. {label}'
            print('\r\033[2K' + line[:max(1, width - 1)])
        sys.stdout.flush()

    draw()
    fd = sys.stdin.fileno()
    original = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd, termios.TCSANOW)
        sys.stdout.write('\033[?25l')
        sys.stdout.flush()
        while True:
            key = os.read(fd, 1)
            if not key:
                raise EOFError
            if key == b'\x03':
                raise KeyboardInterrupt
            if key == b'\x1b':
                sequence = b''
                while len(sequence) < 2 and select.select([fd], [], [], 0.15)[0]:
                    sequence += os.read(fd, 1)
                if sequence in (b'[A', b'OA'):
                    selected = (selected - 1) % len(options)
                elif sequence in (b'[B', b'OB'):
                    selected = (selected + 1) % len(options)
                else:
                    raise WizardCancelled
            elif key in (b'\r', b'\n'):
                print(f'Выбрано: {options[selected]}')
                return selected
            elif key in b'123456789' and int(key) <= len(options):
                selected = int(key) - 1
            else:
                continue
            sys.stdout.write(f'\033[{len(options)}A')
            draw()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, original)
        sys.stdout.write('\033[?25h')
        sys.stdout.flush()


def ask(label, default=None, validate=None):
    while True:
        suffix = f' [{default}]' if default is not None else ''
        value = input(f'{label}{suffix}: ').strip()
        value = value or (str(default) if default is not None else '')
        message = validate(value) if validate else (None if value else 'Ответ не должен быть пустым.')
        if message:
            print(f'  {message}')
        else:
            return value


def normalize_repo(value):
    if value.startswith('https://github.com/'):
        return value.removeprefix('https://github.com/').rstrip('/').removesuffix('.git')
    return value


def slug_problem(value):
    if not VALID_SLUG.fullmatch(value):
        return 'Имя: 2–39 строчных латинских букв, цифр или дефисов; первая — буква.'
    if value == vpskit_vpn.PROJECT:
        return f'Имя {value} зарезервировано для VPN. Выбери другое имя проекта.'
    if (PROJECT_DIR / f'{value}.json').exists():
        return f'Проект {value} уже зарегистрирован. Выбери другое имя.'
    if (KEY_DIR / f'{value}.key').exists() or (KEY_DIR / f'{value}.key.pub').exists():
        return f'Для {value} остался ключ после предыдущей попытки. Проверь {KEY_DIR} или выбери другое имя.'


def repo_problem(value):
    repo = normalize_repo(value)
    if not VALID_REPO.fullmatch(repo) or '..' in repo:
        return 'Укажи OWNER/REPO или ссылку https://github.com/OWNER/REPO.'


def compose_problem(value):
    path = PurePosixPath(value)
    if (not VALID_PATH.fullmatch(value) or path.is_absolute() or '..' in path.parts
            or not path.name or value.endswith('/') or path == PurePosixPath('.')):
        return 'Нужен путь к файлу относительно корня репозитория, например deploy/compose.yaml, без ..'


def branch_problem(value):
    if not VALID_BRANCH.fullmatch(value) or '..' in value or value.startswith(('-', '/')):
        return 'Укажи имя ветки, например main или release/production.'


def port_problem(value):
    if not str(value).isascii() or not str(value).isdigit() or not 1024 <= int(value) <= 65535:
        return 'Порт на VPS должен быть числом от 1024 до 65535.'
    for p in projects():
        if p.get('port') == int(value):
            return f'Порт {value} уже занят проектом {p["slug"]}. Выбери другой.'
    vpn = vpskit_vpn.settings()
    if vpn and vpn['port'] == int(value):
        return f'Порт {value} уже используется Headscale. Выбери другой.'


def domain_problem(value):
    if not VALID_HOST.fullmatch(value) or '..' in value:
        return 'Нужен домен без https:// и пути, например app.example.com.'
    for p in projects():
        if p.get('domain') == value.lower():
            return f'Домен {value} уже используется проектом {p["slug"]}.'
    vpn = vpskit_vpn.settings()
    if vpn and vpn['domain'] == value.lower():
        return f'Домен {value} уже используется Headscale.'


def project_wizard(a):
    require_terminal()
    needs_root()
    settings = load_json(CONFIG)
    print('\nМастер добавления проекта')
    print('Enter принимает значение в скобках. Ctrl+C отменяет ввод.')
    print('Сначала соберём ответы; файлы и ключ будут созданы после подтверждения.')
    while True:
        a.repo = normalize_repo(ask('1/7 Репозиторий GitHub (OWNER/REPO или ссылка)', a.repo, repo_problem))
        suggestion = re.sub(r'[^a-z0-9-]+', '-', a.repo.split('/')[1].lower()).strip('-')[:39]
        if not VALID_SLUG.fullmatch(suggestion):
            suggestion = 'myapp'
        a.slug = ask('2/7 Имя проекта на VPS', a.slug or suggestion, slug_problem)
        print('Compose-файл должен находиться в репозитории приложения.')
        choices = ['compose.yaml', 'docker-compose.yml', 'Другой путь']
        index = choose('3/7 Compose-файл', choices, choices.index(a.compose) if a.compose in choices[:2] else (2 if a.compose else 0))
        a.compose = choices[index] if index < 2 else ask('Путь к Compose-файлу', a.compose, compose_problem)
        a.branch = ask('4/7 Ветка для автодеплоя', a.branch or 'main', branch_problem)
        used = {p['port'] for p in projects()}
        suggested_port = next((port for port in range(8081, 65536) if port not in used), 8081)
        print('Это порт на VPS, а не внутри контейнера. В Compose: 127.0.0.1:ПОРТ:ВНУТРЕННИЙ_ПОРТ.')
        a.port = int(ask('5/7 Порт приложения на VPS', a.port or suggested_port, port_problem))
        domains = ['Свой домен (A-запись должна указывать на VPS)']
        if settings.get('public_ip'):
            domains.insert(0, 'Бесплатный адрес sslip.io по IP сервера')
        auto_domain = f'{a.slug}.{(settings.get("public_ip") or "").replace(".", "-")}.sslip.io'
        index = choose('6/7 Адрес приложения', domains, 1 if len(domains) == 2 and a.domain and a.domain != auto_domain else 0)
        if len(domains) == 2 and index == 0:
            a.domain = auto_domain
            message = domain_problem(a.domain)
            if message:
                print(message)
                continue
        else:
            a.domain = ask('Домен', a.domain, domain_problem).lower()
        a.no_caddy = choose('7/7 Настроить HTTPS через Caddy?', [
            'Создать сайт Caddy для этого проекта',
            'Настрою маршрутизацию сам / сайт уже существует',
        ], int(a.no_caddy)) == 1
        print('\nПроверь перед созданием:')
        for label, value in [('Проект', a.slug), ('Репозиторий', a.repo), ('Ветка', a.branch),
                             ('Compose', a.compose), ('Адрес', f'https://{a.domain}'),
                             ('Порт', f'127.0.0.1:{a.port}'), ('Caddy', 'вручную' if a.no_caddy else 'создать сайт')]:
            print(f'  {label:12} {value}')
        print(f'  Production env: {APPS_DIR / a.slug / "shared/.env"}')
        print('  Будут созданы каталог проекта и отдельный SSH-ключ. Репозиторий GitHub пока не изменится.')
        action = choose('Всё верно?', ['Создать проект', 'Изменить ответы', 'Отмена'])
        if action == 0:
            return a
        if action == 2:
            return None


def write_secret(p, data):
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w') as out:
        out.write(data)


def project_add(a):
    needs_root()
    slug, repo = a.slug, normalize_repo(a.repo)
    for validate, value in [(slug_problem, slug), (repo_problem, repo), (compose_problem, a.compose),
                            (branch_problem, a.branch), (port_problem, a.port)]:
        message = validate(value)
        if message:
            error(message)
    settings = load_json(CONFIG)
    if a.domain:
        domain = a.domain.lower()
    else:
        ip = settings.get('public_ip')
        if not ip:
            error('нет публичного IP в настройках. Укажи --domain или повтори установку с --ip.')
        domain = f'{slug}.{ip.replace(".", "-")}.sslip.io'
    message = domain_problem(domain)
    if message:
        error(message)
    # Guard against overriding an existing site in /etc/caddy/Caddyfile.
    caddyfile = Path('/etc/caddy/Caddyfile')
    if not a.no_caddy and not os.getenv('VPSKIT_CADDY_DIR') and caddyfile.exists() and domain in caddyfile.read_text():
        error(f'домен {domain} уже есть в Caddyfile. Используй --no-caddy и настрой маршрут вручную.')
    site = CADDY_DIR / f'{slug}.caddy'
    if not a.no_caddy and site.exists():
        error(f'файл {site} уже существует. Проверь его или используй --no-caddy.')
    KEY_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(KEY_DIR, 0o700)
    key = KEY_DIR / f'{slug}.key'
    base = APPS_DIR / slug
    shared = base / 'shared'
    releases = base / 'releases'
    env = shared / '.env'
    metadata = PROJECT_DIR / f'{slug}.json'
    created = []
    created_dirs = []
    auth_line = None
    reload_attempted = False
    try:
        # Validate Caddy before generating credentials or registering the project.
        if not a.no_caddy:
            CADDY_DIR.mkdir(parents=True, exist_ok=True)
            write_secret(site, f'{domain} {{\n    reverse_proxy 127.0.0.1:{a.port}\n}}\n')
            created.append(site)
            site.chmod(0o644)
            validation = subprocess.run(['caddy', 'validate', '--config', '/etc/caddy/Caddyfile'], capture_output=True, text=True)
            if validation.returncode:
                error(f'Caddy отклонил конфигурацию: {validation.stderr.strip()}')
        created.extend([key, key.with_suffix('.key.pub')])
        run('ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C', f'vps-kit-{slug}', '-f', str(key))
        os.chmod(key, 0o600)
        pub = key.with_suffix('.key.pub').read_text().strip()
        AUTH_KEYS.parent.mkdir(parents=True, exist_ok=True)
        auth_line = f'command="{RECEIVER} {slug}",restrict {pub}\n'
        with AUTH_KEYS.open('a') as f:
            f.write(auth_line)
        os.chmod(AUTH_KEYS.parent, 0o700)
        os.chmod(AUTH_KEYS, 0o600)
        if os.getenv('VPSKIT_ALLOW_NONROOT') != '1':
            run('chown', '-R', 'deploy:deploy', str(AUTH_KEYS.parent))
        for directory in [base, releases, shared]:
            if not directory.exists():
                directory.mkdir(parents=True)
                created_dirs.append(directory)
        if not env.exists():
            write_secret(env, '')
            created.append(env)
        os.chmod(env, 0o600)
        if os.getenv('VPSKIT_ALLOW_NONROOT') != '1':
            run('chown', '-R', 'deploy:deploy', str(base))
        project = dict(slug=slug, repo=repo, port=a.port, compose=a.compose, branch=a.branch, domain=domain, caddy=not a.no_caddy)
        write_secret(metadata, json.dumps(project, indent=2) + '\n')
        created.append(metadata)
        metadata.chmod(0o644)
        if not a.no_caddy:
            reload_attempted = True
            run('systemctl', 'reload', 'caddy')
    except (Exception, SystemExit, KeyboardInterrupt):
        # Only remove artifacts from this attempt; retain pre-existing app data.
        if auth_line and AUTH_KEYS.exists():
            lines = AUTH_KEYS.read_text().splitlines(keepends=True)
            AUTH_KEYS.write_text(''.join(line for line in lines if line != auth_line))
        for path in reversed(created):
            path.unlink(missing_ok=True)
        for directory in reversed(created_dirs):
            try:
                directory.rmdir()
            except OSError:
                pass  # Never remove pre-existing/nonempty directories recursively.
        if reload_attempted:
            subprocess.run(['systemctl', 'reload', 'caddy'], check=False)
        raise
    print(f'\nПроект создан: {slug} ({repo})')
    print(f'Адрес: https://{domain}')
    print('До первого успешного деплоя сайт может отвечать 502.')
    if a.no_caddy:
        print(f'Caddy не изменён. Настрой маршрут на 127.0.0.1:{a.port} самостоятельно.')
    print(f'В Compose укажи порт: "127.0.0.1:{a.port}:ВНУТРЕННИЙ_ПОРТ"')
    print('\nОсталось выполнить на VPS:')
    print(f'  1. sudo nano {env}  # production-переменные')
    print('  2. sudo gh auth login --web')
    print(f'  3. sudo vpsctl github configure {slug}')
    print('  4. sudo vpsctl registry login YOUR_GITHUB_LOGIN  # для приватных GHCR-образов')
    print(f'  5. vpsctl workflow {slug} --output deploy-{slug}.yml')
    print(f'Затем скопируй deploy-{slug}.yml в репозиторий приложения как .github/workflows/deploy.yml, сделай commit/push.')


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


def project_rm(a):
    needs_root()
    p = read_project(a.slug)
    slug = p['slug']

    if not a.no_confirm:
        confirm = choose(f'Удалить проект {slug} ({p["repo"]})?', ['Удалить', 'Отмена'])
        if confirm == 1:
            print('Отменено.')
            return 0

    caddy = p.get('caddy', True)
    key = KEY_DIR / f'{slug}.key'
    pub = key.with_suffix('.key.pub')
    site = CADDY_DIR / f'{slug}.caddy'
    metadata = PROJECT_DIR / f'{slug}.json'
    auth_line_pattern = f'command="{RECEIVER} {slug}",restrict '
    base = APPS_DIR / slug
    env = base / 'shared' / '.env'
    caddy_reloaded = False

    # Remove authorized_keys entry
    if AUTH_KEYS.exists():
        try:
            lines = AUTH_KEYS.read_text().splitlines(keepends=True)
            filtered = [l for l in lines if auth_line_pattern not in l]
            if len(filtered) != len(lines):
                AUTH_KEYS.write_text(''.join(filtered))
            else:
                print(f'  строка с ключом {slug} в authorized_keys не найдена.')
        except OSError as e:
            print(f'  предупреждение: не удалось обновить authorized_keys: {e}', file=sys.stderr)

    # Remove SSH key pair
    for k in [key, pub]:
        if k.exists():
            k.unlink()
            print(f'  удалён ключ: {k}')

    # Remove Caddy site file
    if caddy and site.exists():
        site.unlink()
        print(f'  удалён сайт Caddy: {site}')
        caddy_reloaded = True

    # Remove metadata
    if metadata.exists():
        metadata.unlink()
        print(f'  удалён метаданные: {metadata}')

    # Remove app directory (base + all contents)
    if base.exists():
        try:
            shutil.rmtree(str(base))
            print(f'  удалён каталог приложения: {base}')
        except OSError as e:
            print(f'  предупреждение: не удалось удалить {base}: {e}', file=sys.stderr)

    # Reload Caddy if needed
    if caddy_reloaded:
        try:
            run('systemctl', 'reload', 'caddy')
            print('  Caddy перезапущен.')
        except subprocess.CalledProcessError as e:
            print(f'  предупреждение: не удалось перезагрузить Caddy: {e}', file=sys.stderr)

    print(f'\nПроект {slug} удалён.')


def project_workflow(a):
    p = read_project(a.slug)
    text = workflow_text(p)
    if getattr(a, 'output', None):
        output = Path(a.output).expanduser()
        try:
            with output.open('x') as f:
                f.write(text)
        except FileExistsError:
            error(f'{output} уже существует. Выбери другой --output; существующий файл не перезаписан.')
        print(f'Workflow сохранён: {output.resolve()}')
        print('Скопируй его в репозиторий приложения: .github/workflows/deploy.yml, затем сделай commit/push.')
    else:
        # stdout must remain pure YAML for existing redirect/automation users.
        sys.stdout.write(text)


def github_configure(a):
    needs_root()
    p = read_project(a.slug)
    key = KEY_DIR / f'{a.slug}.key'
    if not key.exists():
        error(f'временный ключ {key} не найден. Возможно, Secrets уже настроены; повторная загрузка ключа недоступна.')
    if shutil.which('gh') is None:
        error('GitHub CLI (gh) не установлен. Повтори установку vps-kit.')
    if subprocess.run(['gh', 'auth', 'status'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
        error('GitHub CLI не авторизован для root. Выполни: sudo gh auth login --web')
    cfg = load_json(CONFIG)
    host = cfg.get('public_ip')
    if not host:
        error('в настройках нет public_ip. Повтори установку с --ip PUBLIC_IPV4.')
    if not HOST_KEY.exists():
        error('не найден публичный ED25519 host key сервера. Проверь установку sshd.')
    host_key = HOST_KEY.read_text().strip().split()
    if len(host_key) < 2 or host_key[0] != 'ssh-ed25519':
        error('некорректный ED25519 host key сервера.')
    port = int(cfg.get('ssh_port', 22))
    host_entry = host if port == 22 else f'[{host}]:{port}'
    known_hosts = f'{host_entry} {host_key[0]} {host_key[1]}\n'
    secrets = {
        'DEPLOY_HOST': host,
        'DEPLOY_PORT': str(port),
        'DEPLOY_KNOWN_HOSTS': known_hosts,
        'DEPLOY_SSH_KEY': key.read_text(),
    }
    for name, value in secrets.items():
        print(f'Загрузка Secret {name} в {p["repo"]} ...')
        run('gh', 'secret', 'set', name, '--repo', p['repo'], input=value)
    # Key remains only in GitHub Actions secrets and authorized_keys on the VPS.
    key.unlink()
    pub = key.with_suffix('.key.pub')
    if pub.exists(): pub.unlink()
    print('Secrets настроены. Временный приватный ключ удалён с VPS.')
    print(f'Создай workflow: vpsctl workflow {a.slug} --output deploy-{a.slug}.yml')
    print('Для приватных GHCR-образов нужен вход на VPS: sudo vpsctl registry login YOUR_GITHUB_LOGIN')


def registry_login(a):
    needs_root()
    print('Введи GitHub PAT (classic) с read:packages на запрос Docker Password.')
    print('Docker сохранит токен для пользователя deploy на VPS. Не вводи токен в аргументы команды.')
    run('runuser', '-u', 'deploy', '--', 'docker', 'login', 'ghcr.io', '-u', a.user)


def show_status(_):
    registered = projects()
    if not registered:
        print('Проектов пока нет. Добавь первый: sudo vpsctl project add')
    for p in registered:
        print(f'{p["slug"]:20} {p["repo"]:35} {p["domain"]}  -> 127.0.0.1:{p["port"]}')
    if shutil.which('docker'):
        result = subprocess.run(['docker', 'ps', '--format', 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'], capture_output=True, text=True)
        if result.returncode:
            print(result.stderr.strip(), file=sys.stderr)
            print('Не удалось получить контейнеры. Проверь Docker и повтори: sudo vpsctl status', file=sys.stderr)
            return 1
        print(result.stdout, end='')


def doctor(_):
    failed = False
    for name, cmd in [('Docker', ['docker', 'info', '--format', '{{.ServerVersion}}']),
                      ('Compose', ['docker', 'compose', 'version']),
                      ('Caddy', ['systemctl', 'is-active', 'caddy'])]:
        result = subprocess.run(cmd, capture_output=True, text=True)
        failed |= result.returncode != 0
        print(f'[{"OK" if result.returncode == 0 else "ОШИБКА"}] {name}: {(result.stdout or result.stderr).strip()}')
    if shutil.which('curl'):
        result = subprocess.run(['curl', '-LsS', '--max-time', '10', '-o', '/dev/null', '-w', '%{http_code}', 'https://ghcr.io/v2/'], capture_output=True, text=True)
        reachable = result.returncode == 0 and result.stdout.strip() in ('200', '401')
        failed |= not reachable
        print(f'[{"OK" if reachable else "ОШИБКА"}] GHCR: HTTP {result.stdout.strip() or "000"} {result.stderr.strip()}')
        print('HTTP 401 от GHCR ожидаем: это проверка доступности, а не входа в реестр.')
    else:
        failed = True
        print('[ОШИБКА] curl не установлен; доступность GHCR не проверена.')
    print('Проверка не подтверждает права на GHCR-пакеты, DNS, сертификат или готовность приложения.')
    return 1 if failed else 0


def select_project():
    require_terminal()
    registered = projects()
    if not registered:
        error('проектов пока нет. Сначала выполни: sudo vpsctl project add')
    index = choose('Выбери проект', [f'{p["slug"]} — {p["repo"]}' for p in registered])
    return registered[index]['slug']


def build_parser():
    formatter = argparse.RawDescriptionHelpFormatter
    ap = argparse.ArgumentParser(
        prog='vpsctl', formatter_class=formatter,
        description='Приложения и личная VPN на VPS. Docker Compose, GitHub Actions и Headscale.',
        epilog='''Начни здесь:
  sudo vpsctl                         Главное меню: выбор стрелками и Enter
  sudo vpsctl project add             Мастер добавления проекта
  vpsctl help project add             Подробная справка по добавлению
  sudo vpsctl vpn setup               Личная VPN для телефона и ПК

После добавления проекта на VPS:
  1. sudo nano /opt/apps/myapp/shared/.env
  2. sudo gh auth login --web
  3. sudo vpsctl github configure myapp
  4. sudo vpsctl registry login YOUR_GITHUB_LOGIN   (для приватных образов)
  5. vpsctl workflow myapp --output deploy-myapp.yml
Скопируй workflow в репозиторий приложения как .github/workflows/deploy.yml.

Права root: project add/rm, github configure, registry login, vpn. Для doctor рекомендуется sudo.
Любая команда: --help или vpsctl help <команда>. Ctrl+C отменяет интерактивный ввод.
Без терминала команды не задают вопросов; укажи все обязательные параметры.''')
    sub = ap.add_subparsers(dest='command', title='Команды', metavar='КОМАНДА')
    topics = {(): ap}

    def parser(parent, name, title, description, examples, path):
        p = parent.add_parser(name, help=title, description=description, epilog=examples, formatter_class=formatter)
        topics[path] = p
        p.set_defaults(fn=lambda _: p.print_help())
        return p

    proj = parser(sub, 'project', 'Добавление проектов', 'Создай отдельное приложение на VPS.',
                  'sudo vpsctl project add   — мастер\nvpsctl project add --help — все параметры', ('project',))
    ps = proj.add_subparsers(dest='action', title='Действия', metavar='ДЕЙСТВИЕ')
    add = parser(ps, 'add', 'Добавить проект: мастер или параметры',
                 'Создаёт production .env, SSH-ключ и, по выбору, сайт Caddy. Нужен sudo.\nБез обязательных параметров в терминале запускает мастер; существующие проекты не перезаписывает.',
                 '''Примеры:
  sudo vpsctl project add
  sudo vpsctl project add myapp --interactive
  sudo vpsctl project add myapp --repo OWNER/REPO --port 8081
  sudo vpsctl project add myapp --repo OWNER/REPO --port 8081 --domain app.example.com
  sudo vpsctl project add myapp --repo OWNER/REPO --port 8081 --compose deploy/compose.yaml --no-caddy

Мастер: стрелки ↑/↓ или номер и Enter; в простом терминале — номер и Enter.
Перед созданием можно проверить ответы, изменить их или отменить добавление.
Порт в Compose: "127.0.0.1:8081:ВНУТРЕННИЙ_ПОРТ". Production env: /opt/apps/myapp/shared/.env.
Регистрация сама не запускает приложение, не меняет репозиторий и не настраивает DNS/firewall.''', ('project', 'add'))
    add.add_argument('slug', nargs='?', help='имя на VPS: 2–39 строчных латинских букв, цифр/дефисов; первая — буква')
    add.add_argument('--repo', metavar='OWNER/REPO', help='репозиторий приложения; также принимает https://github.com/OWNER/REPO')
    add.add_argument('--port', type=int, metavar='ПОРТ', help='порт на VPS (1024–65535), совпадающий с loopback-портом в Compose')
    add.add_argument('--compose', metavar='ПУТЬ', help='Compose-файл относительно корня репозитория (по умолчанию compose.yaml)')
    add.add_argument('--branch', metavar='ВЕТКА', help='ветка для push-деплоя (по умолчанию main)')
    add.add_argument('--domain', metavar='ДОМЕН', help='свой домен без https://; по умолчанию <slug>.<IP-с-дефисами>.sslip.io')
    add.add_argument('--no-caddy', action='store_true', help='не создавать сайт Caddy; маршрутизацию настроить вручную')
    add.add_argument('-i', '--interactive', action='store_true', help='открыть мастер даже при указанных параметрах; они станут начальными ответами')
    add.set_defaults(fn=project_add)
    rm = parser(ps, 'rm', 'Удалить проект', 'Удаляет все артефакты проекта: метаданные, SSH-ключ, сайт Caddy, каталог приложения. Нужен sudo.',
                 '''Примеры:
sudo vpsctl project rm myapp
sudo vpsctl project rm myapp --no-confirm

Удаляет:
  - /etc/vps-kit/projects/<slug>.json
  - /etc/caddy/vps-kit/<slug>.caddy (если был создан)
  - SSH-ключ из authorized_keys и пару в pending-keys
  - /opt/apps/<slug>/ (с env и RELEASES)
Перед удалением запрашивается подтверждение. С --no-confirm — без запроса.''', ('project', 'rm'))
    rm.add_argument('slug', help='имя проекта на VPS')
    rm.add_argument('--no-confirm', action='store_true', help='удалить без запроса подтверждения')
    rm.set_defaults(fn=project_rm)
    wf = parser(sub, 'workflow', 'Сгенерировать GitHub Actions workflow',
                'Генерирует YAML для репозитория приложения. С именем проекта без --output выводит только YAML в stdout.\nБез имени в терминале предлагает выбрать проект. Сам не делает commit/push и не запускает деплой.',
                '''Примеры:
  vpsctl workflow myapp --output deploy-myapp.yml
  vpsctl workflow myapp > deploy.yml
Скопируй результат в репозиторий приложения как .github/workflows/deploy.yml.
--output не перезаписывает существующие файлы. Символ > подчиняется правилам твоего shell.''', ('workflow',))
    wf.add_argument('slug', nargs='?', help='имя зарегистрированного проекта')
    wf.add_argument('-o', '--output', metavar='ФАЙЛ', help='сохранить YAML в новый файл вместо stdout')
    wf.set_defaults(fn=project_workflow)
    github = parser(sub, 'github', 'Настройка GitHub Secrets', 'Подключение репозитория приложения к VPS.',
                    'sudo gh auth login --web\nsudo vpsctl github configure myapp', ('github',))
    gs = github.add_subparsers(dest='action', title='Действия', metavar='ДЕЙСТВИЕ')
    gc = parser(gs, 'configure', 'Загрузить четыре deployment Secrets',
                'Загружает DEPLOY_HOST, DEPLOY_PORT, DEPLOY_SSH_KEY и DEPLOY_KNOWN_HOSTS. Нужен sudo.\nПредварительно выполни sudo gh auth login --web с правами на Secrets репозитория.',
                '''sudo vpsctl github configure myapp
Без имени в терминале предлагает выбрать проект.
После успешной загрузки временный приватный ключ удаляется с VPS.
При ошибке ключ сохраняется: устрани причину и повтори команду.
После успеха можно выполнить sudo gh auth logout. Вход в GHCR настраивается отдельно.''', ('github', 'configure'))
    gc.add_argument('slug', nargs='?', help='имя зарегистрированного проекта с ещё не загруженным ключом')
    gc.set_defaults(fn=github_configure)
    registry = parser(sub, 'registry', 'Вход VPS в GHCR', 'Доступ VPS к приватным Docker-образам.',
                      'sudo vpsctl registry login YOUR_GITHUB_LOGIN', ('registry',))
    rs = registry.add_subparsers(dest='action', title='Действия', metavar='ДЕЙСТВИЕ')
    lg = parser(rs, 'login', 'Войти в ghcr.io для пользователя deploy',
                'Выполняет Docker login для системного пользователя deploy. Нужен sudo.\nТокен вводится на запрос Password, а не в аргументы команды.',
                '''sudo vpsctl registry login YOUR_GITHUB_LOGIN
Без логина в терминале спрашивает его. Обычно нужен GitHub PAT (classic) с read:packages
и доступом к пакетам приложения; для организации может потребоваться SSO.
Docker хранит токен в конфигурации пользователя deploy без встроенного менеджера секретов.
Для публичных образов анонимный pull может работать без входа.''', ('registry', 'login'))
    lg.add_argument('user', nargs='?', help='логин GitHub, которому принадлежит read:packages токен')
    lg.set_defaults(fn=registry_login)
    vpn = parser(sub, 'vpn', 'Личная VPN: Headscale + свой DERP',
                 'Своя сеть для телефона и ПК с официальными клиентами Tailscale. Нужен sudo.',
                 'sudo vpsctl vpn setup\nsudo vpsctl vpn status\nvpsctl help vpn setup', ('vpn',))
    vs = vpn.add_subparsers(dest='action', title='Действия', metavar='ДЕЙСТВИЕ')
    setup = parser(vs, 'setup', 'Настроить VPN: мастер или параметры',
                   'Запускает Headscale с собственным DERP и HTTPS через существующий Caddy.\n'
                   'Повторный запуск сохраняет конфигурацию, ключи и базу. Firewall и DNS настраиваются отдельно.',
                   '''sudo vpsctl vpn setup
sudo vpsctl vpn setup --domain vpn.example.com
sudo vpsctl vpn setup --domain vpn.example.com --public-ip PUBLIC_IPV4 --user owner --port 18080

Нужны DNS A-запись на VPS, TCP 80/443 и UDP 3478 в host/cloud firewall.
Вместо своего домена можно указать vpn.IP-С-ДЕФИСАМИ.sslip.io.
Без --domain в терминале открывается мастер. С параметрами вопросов нет.
После установки: sudo vpsctl vpn connect — инструкции для телефона и ПК.
При неуспешной проверке HTTPS возвращает код 1, сохраняя запущенный сервер и данные.''', ('vpn', 'setup'))
    setup.add_argument('--domain', help='публичный DNS-домен без https:// и пути')
    setup.add_argument('--public-ip', help='публичный IPv4 VPS; по умолчанию IP из install.sh')
    setup.add_argument('--user', help='имя владельца устройств (по умолчанию owner)')
    setup.add_argument('--port', type=int, help='локальный TCP-порт Headscale (по умолчанию 18080)')
    setup.add_argument('-i', '--interactive', action='store_true', help='открыть мастер с указанными начальными ответами')
    setup.set_defaults(fn=lambda a: vpskit_vpn.setup(a, sys.modules[__name__]))
    for name, title, examples in [
        ('status', 'Контейнер, устройства, VPN-адреса и HTTPS', 'sudo vpsctl vpn status'),
        ('key', 'Создать одноразовый ключ на 1 час', 'sudo vpsctl vpn key'),
        ('register', 'Подтвердить устройство по Auth ID', 'sudo vpsctl vpn register AUTH_ID'),
        ('connect', 'Инструкции для Windows, Android и iPhone', 'sudo vpsctl vpn connect'),
        ('logs', 'Последние 100 строк журнала Headscale', 'sudo vpsctl vpn logs'),
    ]:
        vp = parser(vs, name, title, title + '. Нужен sudo.', examples, ('vpn', name))
        if name == 'register':
            vp.add_argument('auth_id', help='Auth ID со страницы регистрации устройства')
        vp.set_defaults(fn=lambda a, name=name: getattr(vpskit_vpn, name)(a, sys.modules[__name__]))
    st = parser(sub, 'status', 'Показать проекты и запущенные контейнеры',
                'Показывает зарегистрированные приложения: репозиторий, домен и порт; затем docker ps.',
                'vpsctl status\nsudo vpsctl status   # если текущий пользователь не имеет доступа к Docker', ('status',))
    st.set_defaults(fn=show_status)
    doc = parser(sub, 'doctor', 'Проверить Docker, Compose, Caddy и доступность GHCR',
                 'Проверяет инфраструктуру VPS. Рекомендуется sudo; код выхода 1 означает проблему проверки.',
                 '''sudo vpsctl doctor
HTTP 401 от GHCR ожидаем и означает доступность реестра.
Проверка не подтверждает права на конкретный пакет, DNS, TLS или готовность HTTP-приложения.''', ('doctor',))
    doc.set_defaults(fn=doctor)
    hp = parser(sub, 'help', 'Подробная справка по любой команде', 'Справка без выполнения команд.',
                'vpsctl help\nvpsctl help project add\nvpsctl help github configure', ('help',))
    hp.add_argument('topic', nargs='*', metavar='КОМАНДА', help='например project add или registry login')

    def help_topic(a):
        topic = tuple(a.topic)
        if topic not in topics:
            hp.error('неизвестная команда. Посмотри список: vpsctl help')
        topics[topic].print_help()

    hp.set_defaults(fn=help_topic)
    return ap


def main_menu(ap):
    require_terminal()
    print('vps-kit — приложения на твоём VPS')
    while True:
        action = choose('Что сделать?', ['Добавить проект', 'Настроить GitHub Secrets', 'Сохранить workflow',
                                         'Войти в GHCR', 'Показать состояние', 'Проверить VPS', 'Справка', 'Удалить проект',
                                         'Настроить личную VPN (Headscale)', 'Выход'])
        if action == 9:
            return 0
        try:
            if action == 0:
                a = argparse.Namespace(slug=None, repo=None, port=None, compose=None, branch=None, domain=None, no_caddy=False)
                if project_wizard(a):
                    project_add(a)
                else:
                    print('Добавление отменено. Ничего не создано.')
            elif action == 1:
                needs_root()
                slug = select_project()
                if choose(f'Загрузить deployment Secrets в {read_project(slug)["repo"]}?', ['Загрузить Secrets', 'Отмена']) == 0:
                    github_configure(argparse.Namespace(slug=slug))
            elif action == 2:
                slug = select_project()
                output = ask('Куда сохранить workflow?', f'deploy-{slug}.yml')
                project_workflow(argparse.Namespace(slug=slug, output=output))
            elif action == 3:
                needs_root()
                registry_login(argparse.Namespace(user=ask('Логин GitHub')))
            elif action == 4:
                show_status(None)
            elif action == 5:
                doctor(None)
            elif action == 6:
                ap.print_help()
            elif action == 7:
                slug = select_project()
                project_rm(argparse.Namespace(slug=slug, no_confirm=False))
            elif action == 8:
                vpskit_vpn.setup(argparse.Namespace(domain=None, public_ip=None, user=None, port=None, interactive=True),
                                 sys.modules[__name__])
        except SystemExit as e:
            print(e, file=sys.stderr)
        except (WizardCancelled, EOFError, KeyboardInterrupt):
            print('\nОтменено.')
        except (OSError, subprocess.CalledProcessError) as e:
            print(f'Ошибка выполнения: {e}', file=sys.stderr)
        input('\nEnter — вернуться в меню...')


def main():
    ap = build_parser()
    args = ap.parse_args()
    try:
        if not args.command:
            if is_interactive():
                return main_menu(ap)
            ap.print_help()
            return 0
        if args.command == 'project' and args.action == 'add':
            missing = not args.slug or not args.repo or args.port is None
            if args.interactive or (missing and is_interactive()):
                if not project_wizard(args):
                    print('Добавление отменено. Ничего не создано.')
                    return 0
            elif missing:
                ap.error('нужны имя проекта, --repo и --port. Для мастера в терминале: sudo vpsctl project add')
            args.compose = args.compose or 'compose.yaml'
            args.branch = args.branch or 'main'
        elif args.command == 'project' and args.action == 'rm':
            if args.slug is None:
                if not is_interactive():
                    ap.error('укажи имя проекта. Список: vpsctl status')
                args.slug = select_project()
        elif args.command == 'workflow' or (args.command == 'github' and args.action == 'configure'):
            if args.slug is None:
                if not is_interactive():
                    ap.error('укажи имя проекта. Список: vpsctl status')
                args.slug = select_project()
        elif args.command == 'registry' and args.action == 'login' and args.user is None:
            if not is_interactive():
                ap.error('укажи логин GitHub: sudo vpsctl registry login YOUR_GITHUB_LOGIN')
            args.user = ask('Логин GitHub')
        return args.fn(args) or 0
    except WizardCancelled:
        print('\nОтменено.', file=sys.stderr)
        return 0
    except (EOFError, KeyboardInterrupt):
        print('\nВвод отменён.', file=sys.stderr)
        return 130
    except (OSError, subprocess.CalledProcessError) as e:
        error(f'не удалось выполнить команду: {e}')


if __name__ == '__main__':
    sys.exit(main())
