"""Personal Headscale deployment, using vps-kit's existing Docker and Caddy."""
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import tempfile

VERSION = '0.29.7'
PROJECT = 'vps-kit-headscale'
VPN_DIR = Path(os.getenv('VPSKIT_VPN_DIR', '/opt/vps-kit/headscale'))
CADDYFILE = Path(os.getenv('VPSKIT_CADDYFILE', '/etc/caddy/Caddyfile'))
SITE_NAME = 'vps-kit-headscale.caddy'


def fail(message):
    raise SystemExit(f'Ошибка: {message}')


def settings():
    path = VPN_DIR / 'settings.json'
    if not path.exists():
        return None
    try:
        s = json.loads(path.read_text())
        if (not isinstance(s, dict) or not all(isinstance(s.get(key), str)
                for key in ('domain', 'public_ip', 'user', 'version'))
                or type(s.get('port')) is not int):
            raise ValueError('missing settings')
        return s
    except (ValueError, TypeError):
        fail(f'не удалось прочитать {path}. Восстанови настройки из резервной копии.')


def domain_problem(value):
    if len(value) > 253 or '.' not in value or any(
            not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', part)
            for part in value.lower().split('.')):
        return 'Нужен полный DNS-домен без https://, порта и пути.'
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return None
    return 'Укажи DNS-домен, а не IP: клиентам нужен HTTPS-сертификат.'


def ip_problem(value):
    try:
        ip = ipaddress.ip_address(value)
        if ip.version == 4 and ip.is_global:
            return None
    except ValueError:
        pass
    return 'Нужен публичный IPv4 VPS.'


def user_problem(value):
    if not re.fullmatch(r'[a-z][a-z0-9-]{0,31}', value):
        return 'Имя пользователя: 1–32 строчных латинских букв, цифр или дефисов; первая — буква.'


def port_problem(value):
    if not str(value).isascii() or not str(value).isdigit() or not 1024 <= int(value) <= 65535:
        return 'Порт должен быть числом от 1024 до 65535.'


def config_text(s):
    # JSON is also valid YAML and avoids an additional runtime dependency.
    return json.dumps({
        'server_url': f'https://{s["domain"]}',
        'listen_addr': '0.0.0.0:8080',
        'metrics_listen_addr': '127.0.0.1:9090',
        'grpc_listen_addr': '127.0.0.1:50443',
        'noise': {'private_key_path': '/var/lib/headscale/noise_private.key'},
        'prefixes': {'v4': '100.64.0.0/10', 'v6': 'fd7a:115c:a1e0::/48'},
        'derp': {
            'server': {
                'enabled': True, 'region_id': 999, 'region_code': 'vps',
                'region_name': 'My VPS', 'verify_clients': True,
                'stun_listen_addr': '0.0.0.0:3478',
                'private_key_path': '/var/lib/headscale/derp_server_private.key',
                'automatically_add_embedded_derp_region': True,
                'ipv4': s['public_ip'],
            },
            'urls': [], 'paths': [], 'auto_update_enabled': False,
        },
        'disable_check_updates': True,
        'database': {'type': 'sqlite', 'sqlite': {
            'path': '/var/lib/headscale/db.sqlite', 'write_ahead_log': True,
        }},
        'dns': {'magic_dns': True, 'base_domain': 'tailnet.internal',
                'override_local_dns': False, 'nameservers': {'global': [], 'split': {}}},
        'unix_socket': '/var/run/headscale/headscale.sock',
        'unix_socket_permission': '0770',
        'logtail': {'enabled': False},
    }, indent=2) + '\n'


def compose_text(s):
    return json.dumps({'services': {'headscale': {
        'image': f'ghcr.io/juanfont/headscale:{VERSION}',
        'command': ['serve'], 'restart': 'unless-stopped', 'read_only': True,
        'tmpfs': ['/var/run/headscale'],
        'volumes': ['./config:/etc/headscale:ro', './data:/var/lib/headscale'],
        'ports': [f'127.0.0.1:{s["port"]}:8080', '3478:3478/udp'],
        'healthcheck': {'test': ['CMD', 'headscale', 'health'], 'interval': '3s',
                        'timeout': '3s', 'retries': 10, 'start_period': '5s'},
        'logging': {'driver': 'json-file', 'options': {'max-size': '10m', 'max-file': '3'}},
    }}}, indent=2) + '\n'


def site_text(s):
    return (f'http://{s["domain"]} {{\n'
            '    handle /generate_204 {\n        respond 204\n    }\n'
            '    handle {\n        redir https://{host}{uri}\n    }\n}\n'
            f'{s["domain"]} {{\n    reverse_proxy 127.0.0.1:{s["port"]}\n}}\n')


def command(*args, capture=False, check=True):
    result = subprocess.run(args, text=True, capture_output=capture, check=False)
    if check and result.returncode:
        if capture:
            fail((result.stderr or result.stdout).strip() or f'команда {args[0]} завершилась с ошибкой')
        raise subprocess.CalledProcessError(result.returncode, args)
    return result


def compose(*args, **kwargs):
    return command('docker', 'compose', '--project-name', PROJECT,
                   '--file', str(VPN_DIR / 'compose.yaml'), *args, **kwargs)


def headscale(*args, **kwargs):
    return compose('exec', '-T', 'headscale', 'headscale', *args, **kwargs)


def ensure_user(s):
    users = json.loads(headscale('users', 'list', '--output', 'json', capture=True).stdout)
    for user in users or []:
        if user['name'] == s['user']:
            return user['id']
    user = json.loads(headscale('users', 'create', s['user'], '--output', 'json', capture=True).stdout)
    return user['id']


def check_ports(port):
    for kind, address, label in [(socket.SOCK_STREAM, ('127.0.0.1', port), f'TCP {port}'),
                                 (socket.SOCK_DGRAM, ('0.0.0.0', 3478), 'UDP 3478')]:
        with socket.socket(socket.AF_INET, kind) as listener:
            try:
                listener.bind(address)
            except OSError:
                fail(f'{label} уже занят. Освободи порт перед установкой VPN.')


def check_site(s, caddy_dir):
    if not CADDYFILE.is_file() or f'import {caddy_dir}/*.caddy' not in CADDYFILE.read_text():
        fail('Caddyfile не импортирует сайты vps-kit. Сначала повтори установку install.sh.')
    site = caddy_dir / SITE_NAME
    expected = site_text(s)
    if site.exists() and site.read_text() != expected:
        fail(f'{site} уже существует с другой конфигурацией; файл не перезаписан.')
    domain = re.compile(r'(?<![a-zA-Z0-9.-])' + re.escape(s['domain']) + r'(?![a-zA-Z0-9.-])')
    for path in [CADDYFILE, *caddy_dir.glob('*.caddy')]:
        if path != site and domain.search(path.read_text()):
            fail(f'домен {s["domain"]} уже встречается в {path}. Выбери отдельный поддомен.')
    return site


def validate(s, cli):
    for validator, value in [(domain_problem, s['domain']), (ip_problem, s['public_ip']),
                              (user_problem, s['user']), (port_problem, s['port'])]:
        message = validator(value)
        if message:
            fail(message)
    if s['domain'] == 'tailnet.internal':
        fail('tailnet.internal зарезервирован для имён устройств. Выбери публичный домен.')
    for p in cli.projects():
        if p['slug'] == PROJECT or p.get('domain') == s['domain'] or p.get('port') == s['port']:
            fail(f'имя Compose-проекта, домен или порт уже используется проектом {p["slug"]}.')


def wizard(a, cli, previous):
    cli.require_terminal()
    server = cli.load_json(cli.CONFIG)
    print('\nНастройка личной VPN: Headscale + свой DERP + HTTPS через Caddy.')
    print('Enter принимает значение в скобках. До подтверждения файлы не создаются.')
    defaults = previous or {}
    while True:
        a.public_ip = cli.ask('Публичный IPv4 VPS', a.public_ip or defaults.get('public_ip') or server.get('public_ip'), ip_problem)
        a.domain = cli.ask('Домен VPN (DNS A-запись на VPS)', a.domain or defaults.get('domain') or
                           f'vpn.{a.public_ip.replace(".", "-")}.sslip.io', domain_problem).lower()
        a.user = cli.ask('Имя владельца VPN', a.user or defaults.get('user') or 'owner', user_problem)
        a.port = int(cli.ask('Локальный порт Headscale', a.port or defaults.get('port') or 18080, port_problem))
        print(f'\nАдрес: https://{a.domain}; IPv4: {a.public_ip}; пользователь: {a.user}')
        print(f'Headscale: 127.0.0.1:{a.port}; свой DERP/STUN: TCP 443 и UDP 3478.')
        print('Открой TCP 80/443 и UDP 3478 в firewall VPS/провайдера; DNS должен указывать на VPS.')
        choice = cli.choose('Применить настройки?', ['Настроить VPN', 'Изменить ответы', 'Отмена'])
        if choice == 0:
            return True
        if choice == 2:
            return False


def setup(a, cli):
    cli.needs_root()
    previous = settings()
    if a.interactive or (not a.domain and cli.is_interactive() and not previous):
        if not wizard(a, cli, previous):
            print('Настройка VPN отменена. Ничего не создано.')
            return
    if previous:
        for key in ('domain', 'public_ip', 'port', 'user'):
            given = getattr(a, key)
            if key == 'domain' and given:
                given = given.lower()
            if given is not None and given != previous[key]:
                fail(f'VPN уже настроена с {key}={previous[key]}. Настройки и база не перезаписаны.')
        s = previous
        for name in ('compose.yaml', 'config/config.yaml', 'data'):
            if not (VPN_DIR / name).exists():
                fail(f'отсутствует {VPN_DIR / name}. Восстанови файл из резервной копии.')
    else:
        server = cli.load_json(cli.CONFIG)
        s = {'domain': (a.domain or '').lower(), 'public_ip': a.public_ip or server.get('public_ip') or '',
             'port': a.port if a.port is not None else 18080, 'user': a.user if a.user is not None else 'owner', 'version': VERSION}
        if not s['domain']:
            fail('укажи --domain vpn.example.com или запусти sudo vpsctl vpn setup в терминале.')
    validate(s, cli)
    for tool in ('docker', 'caddy', 'systemctl', 'curl'):
        if not shutil.which(tool):
            fail(f'не найден {tool}. Сначала установи vps-kit через install.sh.')
    command('docker', 'info', capture=True)
    command('docker', 'compose', 'version', capture=True)
    site = check_site(s, cli.CADDY_DIR)
    if not previous:
        if VPN_DIR.exists():
            fail(f'{VPN_DIR} уже существует без settings.json; проверь каталог вручную.')
        containers = command('docker', 'ps', '-aq', '--filter', f'label=com.docker.compose.project={PROJECT}', capture=True)
        if containers.stdout.strip():
            fail(f'Compose-проект {PROJECT} уже существует без метаданных vps-kit.')
        check_ports(s['port'])
        VPN_DIR.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.headscale-', dir=VPN_DIR.parent) as tmp:
            stage = Path(tmp) / 'headscale'
            stage.mkdir(mode=0o700)
            (stage / 'config').mkdir(mode=0o755)
            (stage / 'data').mkdir(mode=0o700)
            for name, data in [('settings.json', json.dumps(s, indent=2) + '\n'),
                                ('compose.yaml', compose_text(s)), ('config/config.yaml', config_text(s))]:
                path = stage / name
                path.write_text(data)
                path.chmod(0o600)
            stage.rename(VPN_DIR)
    created_site = False
    reload_attempted = False
    try:
        if not site.exists():
            cli.CADDY_DIR.mkdir(parents=True, exist_ok=True)
            cli.write_secret(site, site_text(s))
            created_site = True
            site.chmod(0o644)
        command('caddy', 'validate', '--config', str(CADDYFILE), capture=True)
        print('Проверка и запуск Headscale…', flush=True)
        compose('pull')
        compose('run', '--rm', '--no-deps', 'headscale', 'configtest')
        compose('up', '-d', '--wait', '--wait-timeout', '45')
        ensure_user(s)
        reload_attempted = True
        command('systemctl', 'reload', 'caddy')
    except (Exception, SystemExit, KeyboardInterrupt):
        if created_site:
            site.unlink(missing_ok=True)
            if reload_attempted:
                command('systemctl', 'reload', 'caddy', check=False)
        print(f'Конфигурация и данные сохранены в {VPN_DIR}. Устрани ошибку и повтори: sudo vpsctl vpn setup', flush=True)
        raise
    print('\nHeadscale запущен. Проверяю публичный HTTPS…', flush=True)
    result = command('curl', '--fail', '--silent', '--show-error', '--max-time', '15',
                     f'https://{s["domain"]}/health', capture=True, check=False)
    instructions(s)
    if result.returncode:
        print(f'HTTPS пока не подтверждён: {result.stderr.strip()}')
        print('Проверь DNS, выпуск сертификата и firewall. Повтори: sudo vpsctl vpn status')
        return 1
    print('Публичный HTTPS /health: OK. Связь телефон ↔ ПК проверяется после регистрации устройств.')


def instructions(s):
    print(f'\nСервер VPN: https://{s["domain"]}')
    print('На VPS нужны TCP 80/443 и UDP 3478 в host/cloud firewall; DNS A-запись должна указывать на VPS.')
    print('Установи Tailscale на ПК и телефон (Android/iOS).')
    print(f'На Windows/Linux: tailscale login --login-server https://{s["domain"]}')
    print('На Windows включи Preferences → Run unattended для доступа до входа пользователя.')
    print('Android: Settings → Accounts → ⋮ → Use an alternate server.')
    print('iPhone: Log in… → меню → Use custom coordination server.')
    print('В приложении укажи адрес сервера выше. После входа откроется страница регистрации.')
    print('Скопируй Auth ID с этой страницы и подтверди каждое устройство на VPS:')
    print('  sudo vpsctl vpn register AUTH_ID')
    print('Или создай отдельный одноразовый ключ на 1 час для каждого устройства:')
    print('  sudo vpsctl vpn key')
    print('Список устройств и их VPN-адреса: sudo vpsctl vpn status')
    print('Проверка с ПК: tailscale status; tailscale ping ИМЯ_ИЛИ_VPN_IP')
    print('Для экрана ПК настрой RDP/другой сервер удалённого рабочего стола и клиент на телефоне.')
    print('Подключайся к VPN-IP ПК; разреши нужный сервис в firewall ПК для VPN-сети.')
    print('ПК должен быть включён и не спать. VPN не включает удалённый рабочий стол автоматически.')


def installed(cli):
    cli.needs_root()
    s = settings()
    if not s:
        fail('VPN ещё не настроена. Запусти: sudo vpsctl vpn setup')
    return s


def register(a, cli):
    s = installed(cli)
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,256}', a.auth_id):
        fail('некорректный Auth ID; скопируй его со страницы регистрации.')
    headscale('auth', 'register', '--user', s['user'], '--auth-id', a.auth_id)


def key(_, cli):
    s = installed(cli)
    uid = ensure_user(s)
    print('Одноразовый ключ, действителен 1 час. Используй для одного устройства:', flush=True)
    headscale('preauthkeys', 'create', '--user', str(uid), '--expiration', '1h')


def status(_, cli):
    s = installed(cli)
    failed = False
    for args in [('ps',), ('exec', '-T', 'headscale', 'headscale', 'nodes', 'list')]:
        failed |= compose(*args, check=False).returncode != 0
    result = command('curl', '--fail', '--silent', '--show-error', '--max-time', '15',
                     f'https://{s["domain"]}/health', capture=True, check=False)
    print(f'HTTPS /health: {"OK" if result.returncode == 0 else result.stderr.strip()}')
    print('STUN UDP 3478, прямое соединение и DERP проверяй на устройствах через tailscale netcheck/ping.')
    return int(failed or result.returncode != 0)


def logs(_, cli):
    installed(cli)
    compose('logs', '--tail', '100', 'headscale')


def connect(_, cli):
    instructions(installed(cli))
