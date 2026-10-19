#!/usr/bin/env python3
"""CLI regression tests. Run on Linux/WSL; no VPS, Docker daemon or GitHub access."""
import fcntl
import io
import json
import os
from pathlib import Path
import pty
import select
import struct
import subprocess
import sys
import tarfile
import tempfile
import termios
import time
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / 'src/vpsctl.py'


class Terminal:
    def __init__(self, args, env):
        self.master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 30, 120, 0, 0))

        def attach():
            os.setsid()
            fcntl.ioctl(slave, termios.TIOCSCTTY, 0)

        self.proc = subprocess.Popen([sys.executable, '-u', str(CLI), *args],
                                     stdin=slave, stdout=slave, stderr=slave, env=env,
                                     preexec_fn=attach)
        os.close(slave)
        self.output = b''
        self.position = 0

    def read_until(self, text):
        needle = text.encode()
        deadline = time.monotonic() + 5
        while needle not in self.output[self.position:]:
            if time.monotonic() > deadline:
                raise AssertionError(f'Terminal did not show {text!r}: {self.output.decode(errors="replace")}')
            if select.select([self.master], [], [], 0.2)[0]:
                try:
                    data = os.read(self.master, 65536)
                except OSError:
                    data = b''
                if not data:
                    raise AssertionError(self.output.decode(errors='replace'))
                self.output += data
        self.position = self.output.index(needle, self.position) + len(needle)

    def send(self, data):
        os.write(self.master, data.encode() if isinstance(data, str) else data)

    def answer(self, prompt, value=''):
        self.read_until(prompt)
        self.send(value + '\n')

    def finish(self):
        result = self.proc.wait(timeout=5)
        while select.select([self.master], [], [], 0)[0]:
            try:
                data = os.read(self.master, 65536)
            except OSError:
                break
            if not data:
                break
            self.output += data
        return result

    def close(self):
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait(timeout=5)
        os.close(self.master)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='vpskit-tests-')
        self.addCleanup(self.temp.cleanup)
        self.p = Path(self.temp.name)
        self.config = self.p / 'config.json'
        self.config.write_text(json.dumps({'public_ip': '198.51.100.10', 'ssh_port': 2222}))
        self.bin = self.p / 'bin'
        self.bin.mkdir()
        self.env = dict(os.environ, TERM='dumb', VPSKIT_ALLOW_NONROOT='1',
                        VPSKIT_CONFIG=str(self.config), VPSKIT_PROJECTS=str(self.p/'projects'),
                        VPSKIT_APPS=str(self.p/'apps'), VPSKIT_KEY_DIR=str(self.p/'keys'),
                        VPSKIT_AUTH_KEYS=str(self.p/'ssh/authorized_keys'),
                        VPSKIT_CADDY_DIR=str(self.p/'caddy'),
                        VPSKIT_HOST_KEY=str(self.p/'host.pub'),
                        PATH=f'{self.bin}:{ROOT}/tests/mockbin:' + os.environ['PATH'])

    def cli(self, *args):
        return subprocess.run([sys.executable, str(CLI), *args], env=self.env,
                              capture_output=True, text=True, timeout=5)

    def terminal(self, *args, arrows=False):
        env = dict(self.env, TERM='xterm' if arrows else 'dumb')
        terminal = Terminal(args, env)
        self.addCleanup(terminal.close)
        return terminal

    def mock(self, name, script):
        path = self.bin / name
        path.write_text('#!/usr/bin/env bash\nset -euo pipefail\n' + script + '\n')
        path.chmod(0o755)

    def add(self, slug='myapp', *flags):
        return self.cli('project', 'add', slug, '--repo', 'Example/MyApp', '--port', '8090', *flags)

    def assert_not_created(self):
        self.assertFalse(list((self.p/'projects').glob('*.json')))
        self.assertFalse(list((self.p/'keys').glob('*.key')))
        self.assertFalse((self.p/'ssh/authorized_keys').exists())
        self.assertFalse((self.p/'apps/myapp/shared/.env').exists())

    def test_help_topics_and_no_terminal(self):
        for topic in [(), ('project',), ('project', 'add'), ('workflow',), ('github',),
                      ('github', 'configure'), ('registry',), ('registry', 'login'), ('status',), ('doctor',)]:
            result = self.cli('help', *topic)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('usage:', result.stdout)
        self.assertEqual(self.cli().returncode, 0)
        self.assertNotEqual(self.cli('project', 'add').returncode, 0)
        self.assertNotEqual(self.cli('project', 'add', '--interactive').returncode, 0)
        self.assertNotEqual(self.cli('workflow').returncode, 0)
        self.assert_not_created()

    def test_wizard_validation_edit_and_create(self):
        (self.p/'projects').mkdir()
        (self.p/'projects/other.json').write_text(json.dumps(dict(slug='other', port=8081, domain='other.example.com')))
        t = self.terminal('project', 'add')
        t.answer('1/7 Репозиторий', 'invalid')
        t.answer('Укажи OWNER/REPO', 'https://github.com/Example/MyApp.git')
        t.answer('2/7 Имя проекта', '../escape')
        t.answer('первая — буква.', 'myapp')
        t.answer('3/7 Compose-файл', '3')
        t.answer('Путь к Compose-файлу', '../outside.yaml')
        t.answer('без ..', 'deploy/compose.yaml')
        t.answer('4/7 Ветка')
        t.answer('5/7 Порт', 'abc')
        t.answer('должен быть числом', '8081')
        t.answer('уже занят проектом', '8090')
        t.answer('6/7 Адрес')
        t.answer('7/7 Настроить', '2')
        t.answer('Всё верно?', '2')
        self.assertFalse((self.p/'projects/myapp.json').exists())
        self.assertFalse((self.p/'keys').exists())
        for prompt in ['1/7 Репозиторий', '2/7 Имя проекта', '3/7 Compose-файл', 'Путь к Compose-файлу',
                       '4/7 Ветка', '5/7 Порт', '6/7 Адрес', '7/7 Настроить', 'Всё верно?']:
            t.answer(prompt)
        self.assertEqual(t.finish(), 0, t.output.decode())
        project = json.loads((self.p/'projects/myapp.json').read_text())
        self.assertEqual(project['compose'], 'deploy/compose.yaml')
        self.assertEqual(project['repo'], 'Example/MyApp')
        self.assertEqual(project['port'], 8090)
        self.assertFalse(project['caddy'])
        self.assertEqual((self.p/'apps/myapp/shared/.env').stat().st_mode & 0o777, 0o600)

    def test_wizard_cancel_confirmation(self):
        t = self.terminal('project', 'add')
        for prompt, answer in [('1/7 Репозиторий', 'Example/MyApp'), ('2/7 Имя проекта', ''),
                               ('3/7 Compose-файл', ''), ('4/7 Ветка', ''), ('5/7 Порт', ''),
                               ('6/7 Адрес', ''), ('7/7 Настроить', ''), ('Всё верно?', '3')]:
            t.answer(prompt, answer)
        self.assertEqual(t.finish(), 0)
        self.assert_not_created()

    def test_menu_arrow_exit(self):
        t = self.terminal(arrows=True)
        t.read_until('\x1b[?25l')
        t.send(b'\x1b[A\r')  # Wrap from first option to Exit.
        self.assertEqual(t.finish(), 0, t.output.decode())
        self.assertIn(b'\x1b[?25h', t.output)
        self.assert_not_created()

    def test_arrow_wizard_custom_domain(self):
        t = self.terminal('project', 'add', 'myapp', '--repo', 'Example/MyApp', '--port', '8090', '--interactive', arrows=True)
        t.answer('1/7 Репозиторий')
        t.answer('2/7 Имя проекта')
        t.read_until('\x1b[?25l'); t.send(b'\x1b[B\r')
        t.answer('4/7 Ветка', 'release/production')
        t.answer('5/7 Порт')
        t.read_until('\x1b[?25l'); t.send(b'\x1b[B\r')
        t.answer('Домен:', 'APP.EXAMPLE.COM')
        t.read_until('\x1b[?25l'); t.send(b'\x1b[B\r')
        t.read_until('\x1b[?25l'); t.send(b'\r')
        self.assertEqual(t.finish(), 0, t.output.decode())
        p = json.loads((self.p/'projects/myapp.json').read_text())
        self.assertEqual(p['compose'], 'docker-compose.yml')
        self.assertEqual(p['domain'], 'app.example.com')
        self.assertEqual(p['branch'], 'release/production')

    def test_keyboard_cancel_restores_terminal(self):
        for key, code in [(b'\x1b', 0), (b'\x03', 130)]:
            t = self.terminal('project', 'add', arrows=True)
            t.answer('1/7 Репозиторий', 'Example/MyApp')
            t.answer('2/7 Имя проекта')
            t.read_until('\x1b[?25l'); t.send(key)
            self.assertEqual(t.finish(), code, t.output.decode())
            self.assertIn(b'\x1b[?25h', t.output)
            self.assert_not_created()

    def test_workflow_stdout_and_safe_output(self):
        self.assertEqual(self.add('myapp', '--no-caddy').returncode, 0)
        result = self.cli('workflow', 'myapp')
        self.assertEqual(yaml.safe_load(result.stdout)['jobs']['deploy']['runs-on'], 'ubuntu-latest')
        output = self.p/'deploy.yml'
        self.assertEqual(self.cli('workflow', 'myapp', '-o', str(output)).returncode, 0)
        first = output.read_text()
        self.assertNotEqual(self.cli('workflow', 'myapp', '-o', str(output)).returncode, 0)
        self.assertEqual(first, output.read_text())
        self.assertNotEqual(self.add('myapp', '--no-caddy').returncode, 0)

    def test_invalid_caddy_can_retry(self):
        self.mock('caddy', 'if [[ "${MOCK_CADDY_FAIL:-0}" == 1 ]]; then echo "invalid config" >&2; exit 1; fi')
        self.mock('systemctl', 'exit 0')
        self.env['MOCK_CADDY_FAIL'] = '1'
        self.assertNotEqual(self.add().returncode, 0)
        self.assert_not_created()
        self.assertFalse((self.p/'caddy/myapp.caddy').exists())
        self.env['MOCK_CADDY_FAIL'] = '0'
        self.assertEqual(self.add().returncode, 0)

    def test_reload_failure_keeps_existing_data_and_keys(self):
        shared = self.p/'apps/myapp/shared'
        shared.mkdir(parents=True)
        (shared/'.env').write_text('EXISTING=keep-me\n')
        auth = self.p/'ssh/authorized_keys'
        auth.parent.mkdir()
        auth.write_text('ssh-ed25519 EXISTING_KEY original\n')
        self.mock('caddy', 'exit 0')
        self.mock('systemctl', 'exit 1')
        self.assertNotEqual(self.add().returncode, 0)
        self.assertEqual((shared/'.env').read_text(), 'EXISTING=keep-me\n')
        self.assertEqual(auth.read_text(), 'ssh-ed25519 EXISTING_KEY original\n')
        self.assertFalse((self.p/'projects/myapp.json').exists())
        self.assertFalse((self.p/'keys/myapp.key').exists())
        self.assertFalse((self.p/'caddy/myapp.caddy').exists())

    def test_github_known_hosts_and_key_cleanup(self):
        self.mock('gh', 'if [[ "$1" == auth ]]; then exit 0; fi\ncat > "$MOCK_SECRETS/$3"')
        self.env['MOCK_SECRETS'] = str(self.p/'secrets')
        (self.p/'secrets').mkdir()
        (self.p/'host.pub').write_text('ssh-ed25519 TEST_HOST_PUBLIC_KEY host\n')
        for port in [2222, 22]:
            self.config.write_text(json.dumps({'public_ip': '198.51.100.10', 'ssh_port': port}))
            slug = f'app{port}'
            self.assertEqual(self.add(slug, '--no-caddy').returncode, 0)
            result = self.cli('github', 'configure', slug)
            self.assertEqual(result.returncode, 0, result.stderr)
            host = '198.51.100.10' if port == 22 else '[198.51.100.10]:2222'
            self.assertTrue((self.p/'secrets/DEPLOY_KNOWN_HOSTS').read_text().startswith(host + ' '))
            self.assertEqual((self.p/'secrets/DEPLOY_PORT').read_text(), str(port))
            self.assertEqual((self.p/'secrets/DEPLOY_HOST').read_text(), '198.51.100.10')
            self.assertFalse((self.p/f'keys/{slug}.key').exists())
            (self.p/f'projects/{slug}.json').unlink()  # Reuse the same port fixture.

    def test_doctor_reports_failure_exit_code(self):
        self.mock('docker', 'echo "test docker"; exit "${MOCK_DOCKER_FAIL:-0}"')
        self.mock('systemctl', 'echo active')
        self.mock('curl', 'printf 401')
        self.assertEqual(self.cli('doctor').returncode, 0)
        self.env['MOCK_DOCKER_FAIL'] = '1'
        self.assertEqual(self.cli('doctor').returncode, 1)

    def test_status_failure_explains_sudo(self):
        self.mock('docker', 'echo "docker socket denied" >&2; exit 1')
        result = self.cli('status')
        self.assertEqual(result.returncode, 1)
        self.assertIn('sudo vpsctl status', result.stderr)

    def test_no_ip_wizard_uses_custom_domain(self):
        self.config.write_text('{"public_ip":null,"ssh_port":22}')
        t = self.terminal('project', 'add')
        for prompt, answer in [('1/7 Репозиторий', 'Example/MyApp'), ('2/7 Имя проекта', ''),
                               ('3/7 Compose-файл', ''), ('4/7 Ветка', ''), ('5/7 Порт', ''),
                               ('6/7 Адрес', ''), ('Домен:', 'app.example.com'),
                               ('7/7 Настроить', '2'), ('Всё верно?', '')]:
            t.answer(prompt, answer)
        self.assertEqual(t.finish(), 0, t.output.decode())
        self.assertEqual(json.loads((self.p/'projects/myapp.json').read_text())['domain'], 'app.example.com')


class DeploymentTests(unittest.TestCase):
    def test_port_policy(self):
        guard = (ROOT/'src/receive-deploy.sh').read_text().split("| python3 -c '\n", 1)[1].split("\n'", 1)[0]
        for service, allowed in [({'network_mode': 'host'}, False),
                                 ({'ports': [{'host_ip': '0.0.0.0'}]}, False),
                                 ({'ports': [{'target': 80}]}, False),
                                 ({'ports': [{'host_ip': '127.0.0.1'}]}, True),
                                 ({'ports': [{'host_ip': '::1'}]}, True), ({}, True)]:
            result = subprocess.run([sys.executable, '-c', guard], input=json.dumps({'services': {'web': service}}),
                                    text=True, capture_output=True)
            self.assertEqual(result.returncode == 0, allowed, result.stderr)

    def test_extracted_directory_permissions_under_receiver_umask(self):
        with tempfile.TemporaryDirectory() as temp:
            data = io.BytesIO()
            with tarfile.open(fileobj=data, mode='w:gz') as archive:
                directory = tarfile.TarInfo('public'); directory.type = tarfile.DIRTYPE; directory.mode = 0o755
                archive.addfile(directory)
                for name in ['public/index.html', 'implicit/subdir/file.txt']:
                    entry = tarfile.TarInfo(name); entry.size = 2; entry.mode = 0o644
                    archive.addfile(entry, io.BytesIO(b'ok'))
            result = subprocess.run([sys.executable, str(ROOT/'src/extract-bundle.py'), temp], input=data.getvalue(),
                                    capture_output=True, preexec_fn=lambda: os.umask(0o077))
            self.assertEqual(result.returncode, 0, result.stderr)
            for directory in ['public', 'implicit', 'implicit/subdir']:
                self.assertEqual((Path(temp)/directory).stat().st_mode & 0o777, 0o755)

    def test_reinstall_preserves_connection_defaults(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp)/'config.json'
            config.write_text('{"public_ip":"198.51.100.10","ssh_port":2222}')
            script = (ROOT/'src/install-base.sh').read_text().split('# Preserve explicitly configured connection settings on subsequent installs.\n', 1)[1].split('. /etc/os-release', 1)[0]
            script = script.replace('/etc/vps-kit/config.json', str(config))
            for given_ip, given_port, expected in [('', '', '198.51.100.10 2222'),
                                                   ('203.0.113.20', '2200', '203.0.113.20 2200')]:
                result = subprocess.run(['bash', '-c', 'set -euo pipefail\n' + script + '\nprintf "%s %s" "$PUBLIC_IP" "$SSH_PORT"'],
                                        env=dict(os.environ, PUBLIC_IP=given_ip, SSH_PORT=given_port),
                                        text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, expected)


if __name__ == '__main__':
    unittest.main()
