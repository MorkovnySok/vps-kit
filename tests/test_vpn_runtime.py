#!/usr/bin/env python3
"""Opt-in Docker smoke test: VPSKIT_RUN_DOCKER_TESTS=1 python3 tests/test_vpn_runtime.py -v.

Uses temporary containers/ports/data. Does not test public TLS or phone/PC connectivity.
"""
import io
import json
import os
from pathlib import Path
import secrets
import socket
import struct
import subprocess
import sys
import tarfile
import tempfile
import unittest
import urllib.request
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import vpskit_vpn as vpn


@unittest.skipUnless(os.getenv('VPSKIT_RUN_DOCKER_TESTS') == '1', 'opt-in: requires Docker and image downloads')
class RuntimeTests(unittest.TestCase):
    def test_headscale_config_health_stun_user_keys_and_restart(self):
        with tempfile.TemporaryDirectory(prefix='vpskit-vpn-runtime-') as tmp:
            base = Path(tmp)
            (base / 'config').mkdir()
            (base / 'data').mkdir()
            s = dict(domain='vpn.example.test', public_ip='127.0.0.1', port=18080, user='owner')
            (base / 'config/config.yaml').write_text(vpn.config_text(s))
            compose = json.loads(vpn.compose_text(s))
            compose['services']['headscale']['ports'] = ['127.0.0.1::8080', '127.0.0.1::3478/udp']
            (base / 'compose.yaml').write_text(json.dumps(compose))
            prefix = ['docker', 'compose', '--project-name', 'vpskit-vpn-test-' + secrets.token_hex(4),
                      '--file', str(base / 'compose.yaml')]

            def dc(*args, capture=True):
                result = subprocess.run([*prefix, *args], text=True, capture_output=capture, timeout=90)
                self.assertEqual(result.returncode, 0, (result.stderr or '') + (result.stdout or ''))
                return result.stdout

            def hs(*args):
                return dc('exec', '-T', 'headscale', 'headscale', *args)

            def identity():
                # Server keys are intentionally root-only on a native Linux host.
                container = dc('ps', '-q', 'headscale').strip()
                result = subprocess.run(['docker', 'cp', f'{container}:/var/lib/headscale/noise_private.key', '-'],
                                        capture_output=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr.decode())
                with tarfile.open(fileobj=io.BytesIO(result.stdout)) as archive:
                    return archive.extractfile(archive.getmembers()[0]).read()

            try:
                dc('run', '--rm', '--no-deps', 'headscale', 'configtest')
                dc('up', '-d', '--wait', '--wait-timeout', '45')
                hs('health')
                port = int(dc('port', 'headscale', '8080').strip().rsplit(':', 1)[1])
                with urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=5) as response:
                    self.assertEqual(response.status, 200)
                udpport = int(dc('port', '--protocol', 'udp', 'headscale', '3478').strip().rsplit(':', 1)[1])
                transaction = secrets.token_bytes(12)
                # Tailscale STUN requires its SOFTWARE attribute and FINGERPRINT.
                packet = struct.pack('!HHI', 1, 20, 0x2112A442) + transaction
                packet += struct.pack('!HH', 0x8022, 8) + b'tailnode'
                packet += struct.pack('!HHI', 0x8028, 4, zlib.crc32(packet) ^ 0x5354554e)
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
                    client.settimeout(5)
                    client.sendto(packet, ('127.0.0.1', udpport))
                    reply, _ = client.recvfrom(4096)
                self.assertEqual(reply[:2], b'\x01\x01')
                self.assertEqual(reply[4:20], packet[4:20])
                user = json.loads(hs('users', 'create', 'owner', '--output', 'json'))
                uid = str(user['id'])
                key = json.loads(hs('preauthkeys', 'create', '--user', uid, '--expiration', '1h', '--output', 'json'))
                self.assertFalse(key.get('reusable', False))
                self.assertTrue(key['key'])
                original_identity = identity()
                dc('restart', 'headscale')
                dc('up', '-d', '--wait', '--wait-timeout', '45')
                users = json.loads(hs('users', 'list', '--output', 'json'))
                self.assertEqual(str(users[0]['id']), uid)
                self.assertEqual(identity(), original_identity)
                keys = json.loads(hs('preauthkeys', 'list', '--output', 'json'))
                self.assertEqual(keys[0]['id'], key['id'])
                (base / 'Caddyfile').write_text(vpn.site_text(s))
                result = subprocess.run(['docker', 'run', '--rm', '--volume',
                                         f'{base / "Caddyfile"}:/etc/caddy/Caddyfile:ro',
                                         'caddy:2', 'caddy', 'validate', '--config', '/etc/caddy/Caddyfile'],
                                        text=True, capture_output=True, timeout=90)
                self.assertEqual(result.returncode, 0, result.stderr)
            finally:
                subprocess.run([*prefix, 'down', '--volumes'], capture_output=True, timeout=30)


if __name__ == '__main__':
    unittest.main()
