#!/usr/bin/env python3
"""Offline VPN provisioning and failure recovery; run on Linux/WSL."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest

from test_cli import Terminal

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / 'src/vpsctl.py'

MOCK = '''import json, os, pathlib, sys
tool = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ['MOCK_LOG'], 'a') as out:
    out.write(json.dumps([tool, *args]) + '\\n')
if tool == 'docker':
    if args[:2] == ['ps', '-aq']:
        print(os.getenv('UNMANAGED_CONTAINER', ''))
    if 'users' in args and 'list' in args:
        print('[{"name":"owner","id":7}]' if os.getenv('USER_EXISTS') else '[]')
    if 'users' in args and 'create' in args:
        print('{"name":"owner","id":7}')
    if 'up' in args:
        data = pathlib.Path(os.environ['VPSKIT_VPN_DIR']) / 'data'
        (data / 'db.sqlite').write_text('precious database')
        (data / 'noise_private.key').write_text('precious key')
    if os.getenv('FAIL_DOCKER') and os.environ['FAIL_DOCKER'] in args:
        sys.exit(1)
if tool == 'caddy' and os.getenv('FAIL_CADDY'):
    print('invalid caddy config', file=sys.stderr)
    sys.exit(1)
if tool == 'systemctl' and os.getenv('FAIL_RELOAD'):
    sys.exit(1)
if tool == 'curl' and os.getenv('FAIL_HTTPS'):
    print('certificate not ready', file=sys.stderr)
    sys.exit(60)
'''


class VpnTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='vpskit-vpn-tests-')
        self.addCleanup(self.temp.cleanup)
        self.p = Path(self.temp.name)
        self.vpn = self.p / 'headscale'
        self.caddy = self.p / 'sites'
        self.caddy.mkdir()
        self.site = self.caddy / 'vps-kit-headscale.caddy'
        self.caddyfile = self.p / 'Caddyfile'
        self.caddyfile.write_text(f'import {self.caddy}/*.caddy\n')
        self.config = self.p / 'config.json'
        self.config.write_text('{"public_ip":"93.184.216.34","ssh_port":22}')
        self.bin = self.p / 'bin'
        self.bin.mkdir()
        for name in ('docker', 'caddy', 'systemctl', 'curl'):
            path = self.bin / name
            path.write_text(f'#!{sys.executable}\n' + MOCK)
            path.chmod(0o755)
        self.env = dict(os.environ, TERM='dumb', VPSKIT_ALLOW_NONROOT='1',
                        VPSKIT_CONFIG=str(self.config), VPSKIT_PROJECTS=str(self.p / 'projects'),
                        VPSKIT_APPS=str(self.p / 'apps'), VPSKIT_KEY_DIR=str(self.p / 'keys'),
                        VPSKIT_AUTH_KEYS=str(self.p / 'authorized_keys'),
                        VPSKIT_VPN_DIR=str(self.vpn), VPSKIT_CADDY_DIR=str(self.caddy),
                        VPSKIT_CADDYFILE=str(self.caddyfile), MOCK_LOG=str(self.p / 'commands'),
                        PATH=f'{self.bin}:{ROOT}/tests/mockbin:' + os.environ['PATH'])

    def cli(self, *args):
        return subprocess.run([sys.executable, str(CLI), *args], env=self.env,
                              capture_output=True, text=True, timeout=10)

    def setup(self, *args):
        return self.cli('vpn', 'setup', '--domain', 'vpn.example.com', *args)

    def calls(self):
        path = self.p / 'commands'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_install_and_repeated_setup_preserve_data_and_config(self):
        result = self.setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        config = json.loads((self.vpn / 'config/config.yaml').read_text())
        self.assertEqual(config['derp']['urls'], [])
        self.assertTrue(config['derp']['server']['verify_clients'])
        self.assertEqual(config['derp']['server']['ipv4'], '93.184.216.34')
        self.assertFalse(config['dns']['override_local_dns'])
        compose = json.loads((self.vpn / 'compose.yaml').read_text())['services']['headscale']
        self.assertEqual(compose['image'], 'ghcr.io/juanfont/headscale:0.29.7')
        self.assertEqual(compose['ports'], ['127.0.0.1:18080:8080', '3478:3478/udp'])
        self.assertNotIn('9090:9090', str(compose))
        self.assertEqual(self.vpn.stat().st_mode & 0o777, 0o700)
        configpath = self.vpn / 'config/config.yaml'
        self.assertEqual(configpath.stat().st_mode & 0o777, 0o600)
        configpath.write_text(configpath.read_text() + '\n')  # Local customization survives reruns.
        before = {str(p): p.read_bytes() for p in self.vpn.rglob('*') if p.is_file()}
        self.env['USER_EXISTS'] = '1'
        result = self.cli('vpn', 'setup')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.vpn.rglob('*') if p.is_file()})
        creates = [c for c in self.calls() if 'users' in c and 'create' in c]
        self.assertEqual(len(creates), 1)
        self.assertIn('vpn register AUTH_ID', result.stdout)

    def test_invalid_input_never_creates_files(self):
        for args in [('--domain', 'https://bad.example.com'), ('--domain', '127.0.0.1'),
                     ('--domain', '-bad.example.com'), ('--domain', 'tailnet.internal'),
                     ('--user', '../escape'), ('--public-ip', '10.0.0.1'),
                     ('--port', '80'), ('--port', '0')]:
            with self.subTest(args=args):
                result = self.setup(*args)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.vpn.exists())
                self.assertFalse(self.site.exists())
        self.assertNotEqual(self.cli('vpn', 'setup').returncode, 0)
        self.assertEqual(self.calls(), [])

    def test_foreign_caddy_site_or_missing_import_is_untouched(self):
        original = 'vpn.example.com {\n    respond "existing"\n}\n'
        site = self.caddy / 'other.caddy'
        site.write_text(original)
        self.assertNotEqual(self.setup().returncode, 0)
        self.assertEqual(site.read_text(), original)
        self.assertFalse(self.vpn.exists())
        site.unlink()
        self.caddyfile.write_text('# unmanaged Caddyfile\n')
        self.assertNotEqual(self.setup().returncode, 0)
        self.assertFalse(self.vpn.exists())

    def test_project_collisions_and_reserved_vpn_domain_port(self):
        projects = self.p / 'projects'
        projects.mkdir()
        path = projects / 'myapp.json'
        for domain, port in [('vpn.example.com', 8081), ('app.example.com', 18080)]:
            path.write_text(json.dumps(dict(slug='myapp', domain=domain, port=port)))
            self.assertNotEqual(self.setup().returncode, 0)
            self.assertFalse(self.vpn.exists())
        path.write_text(json.dumps(dict(slug='vps-kit-headscale', domain='other.example.com', port=8081)))
        self.assertNotEqual(self.setup().returncode, 0)
        self.assertFalse(self.vpn.exists())
        path.unlink()
        reserved = self.cli('project', 'add', 'vps-kit-headscale', '--repo', 'Example/MyApp',
                            '--port', '8081', '--domain', 'app.example.com', '--no-caddy')
        self.assertNotEqual(reserved.returncode, 0)
        self.assertFalse((projects / 'vps-kit-headscale.json').exists())
        self.assertEqual(self.setup().returncode, 0)
        for domain, port in [('vpn.example.com', '8081'), ('app.example.com', '18080')]:
            result = self.cli('project', 'add', 'myapp', '--repo', 'Example/MyApp',
                              '--port', port, '--domain', domain, '--no-caddy')
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(path.exists())

    def test_busy_port_and_unmanaged_container(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            result = self.setup('--port', str(listener.getsockname()[1]))
            self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.vpn.exists())
        self.env['UNMANAGED_CONTAINER'] = 'unmanaged-container-id'
        self.assertNotEqual(self.setup().returncode, 0)
        self.assertFalse(self.vpn.exists())

    def test_failures_remove_only_new_caddy_site_and_allow_retry(self):
        for failure, value in [('FAIL_CADDY', '1'), ('FAIL_DOCKER', 'configtest'),
                                ('FAIL_DOCKER', 'up'), ('FAIL_RELOAD', '1')]:
            with self.subTest(failure=failure, value=value):
                self.env[failure] = value
                result = self.setup()
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.site.exists())
                self.assertTrue((self.vpn / 'settings.json').exists())
                self.assertFalse(any('down' in c for c in self.calls()))
                del self.env[failure]
                self.assertEqual(self.setup().returncode, 0)
                # A subsequent failing attempt must leave the existing site intact.
                self.env[failure] = value
                original = self.site.read_text()
                self.assertNotEqual(self.setup().returncode, 0)
                self.assertEqual(self.site.read_text(), original)
                del self.env[failure]
                self.site.unlink()

    def test_https_failure_keeps_server_and_returns_nonzero(self):
        self.env['FAIL_HTTPS'] = '1'
        result = self.setup()
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(self.site.exists())
        self.assertTrue((self.vpn / 'data/db.sqlite').exists())
        self.assertIn('HTTPS пока не подтверждён', result.stdout)
        self.assertNotEqual(self.cli('vpn', 'status').returncode, 0)

    def test_settings_changes_and_damaged_metadata_are_not_overwritten(self):
        self.assertEqual(self.setup().returncode, 0)
        for args in [('--domain', 'another.example.com'), ('--user', 'other'),
                     ('--port', '18081'), ('--public-ip', '1.1.1.1')]:
            self.assertNotEqual(self.setup(*args).returncode, 0)
        self.assertEqual(self.setup('--domain', 'VPN.EXAMPLE.COM').returncode, 0)
        (self.vpn / 'settings.json').write_text('{}')
        result = self.cli('vpn', 'setup')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('Traceback', result.stderr)
        self.assertEqual((self.vpn / 'data/db.sqlite').read_text(), 'precious database')

    def test_key_register_status_logs_and_missing_install(self):
        self.assertNotEqual(self.cli('vpn', 'key').returncode, 0)
        self.assertEqual(self.setup().returncode, 0)
        self.env['USER_EXISTS'] = '1'
        for args in [('key',), ('register', 'test-auth-id'), ('status',), ('logs',), ('connect',)]:
            result = self.cli('vpn', *args)
            self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        key = next(c for c in calls if 'preauthkeys' in c)
        self.assertEqual(key[-4:], ['--user', '7', '--expiration', '1h'])
        self.assertNotIn('--reusable', key)
        register = next(c for c in calls if 'register' in c)
        self.assertEqual(register[-4:], ['--user', 'owner', '--auth-id', 'test-auth-id'])
        self.env['FAIL_DOCKER'] = 'exec'
        self.assertNotEqual(self.cli('vpn', 'status').returncode, 0)

    def test_wizard_cancel_creates_nothing(self):
        terminal = Terminal(['vpn', 'setup'], self.env)
        self.addCleanup(terminal.close)
        for prompt, answer in [('Публичный IPv4', ''), ('Домен VPN', ''),
                               ('Имя владельца', ''), ('Локальный порт', ''), ('Применить настройки?', '3')]:
            terminal.answer(prompt, answer)
        self.assertEqual(terminal.finish(), 0, terminal.output.decode())
        self.assertFalse(self.vpn.exists())
        self.assertFalse(self.site.exists())
        self.assertEqual(self.calls(), [])

    def test_wizard_validates_and_waits_for_confirmation(self):
        terminal = Terminal(['vpn', 'setup'], self.env)
        self.addCleanup(terminal.close)
        terminal.answer('Публичный IPv4', '10.0.0.1')
        terminal.answer('Нужен публичный IPv4', '93.184.216.34')
        terminal.answer('Домен VPN', 'VPN.EXAMPLE.COM')
        terminal.answer('Имя владельца')
        terminal.answer('Локальный порт')
        terminal.read_until('Применить настройки?')
        self.assertFalse(self.vpn.exists())
        self.assertFalse(self.site.exists())
        terminal.send('\n')
        self.assertEqual(terminal.finish(), 0, terminal.output.decode())
        self.assertEqual(json.loads((self.vpn / 'settings.json').read_text())['domain'], 'vpn.example.com')

    def test_built_installer_includes_working_vpn_module(self):
        script = (ROOT / 'install.sh').read_text()
        binary = self.p / 'installed/vpsctl'
        library = self.p / 'installed/lib/vpskit_vpn.py'
        library.parent.mkdir(parents=True)
        for delimiter, path in [('VPSCTL_SCRIPT', binary), ('VPN_SCRIPT', library)]:
            content = script.split(f"<<'{delimiter}'\n", 1)[1].split(f'\n{delimiter}\n', 1)[0]
            path.write_text(content)
        result = subprocess.run([sys.executable, str(binary), 'help', 'vpn', 'setup'],
                                env=dict(self.env, VPSKIT_LIBEXEC=str(library.parent)),
                                text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Headscale', result.stdout)


if __name__ == '__main__':
    unittest.main()
