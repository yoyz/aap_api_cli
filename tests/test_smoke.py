#!/usr/bin/env python3
"""Basic smoke tests for aap_api_cli.py.

Runs against tests/mock_aap.py on a free port, with a temporary config.
Standard library only; pytest will also collect these unittest cases.

    python3 tests/test_smoke.py
    python3 -m unittest discover -s tests -v
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CLI = [sys.executable, os.path.join(ROOT, 'aap_api_cli.py')]
MOCK = os.path.join(HERE, 'mock_aap.py')


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


class SmokeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix='aap-smoke-')
        cls.port = free_port()
        cls.log_path = os.path.join(cls.tmpdir, 'mock.log')
        cls.log_handle = open(cls.log_path, 'w', encoding='utf-8')
        cls.mock = subprocess.Popen(
            [sys.executable, MOCK, str(cls.port)],
            stdout=cls.log_handle, stderr=subprocess.STDOUT)

        ping = 'http://127.0.0.1:%d/api/gateway/v1/ping/' % cls.port
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        deadline = time.time() + 10
        ready = False
        while time.time() < deadline:
            if cls.mock.poll() is not None:
                break
            try:
                with opener.open(ping, timeout=0.5) as response:
                    if response.getcode() == 200:
                        ready = True
                        break
            except (urllib.error.URLError, OSError):
                pass
            time.sleep(0.05)
        if not ready:
            log = open(cls.log_path, encoding='utf-8', errors='replace').read()
            cls.tearDownClass()
            raise RuntimeError('mock did not start\n%s' % log)

        cls.config = os.path.join(cls.tmpdir, 'smoke.conf')
        with open(cls.config, 'w', encoding='utf-8') as handle:
            handle.write(
                '[smoke]\n'
                'host = http://127.0.0.1:%d\n'
                'user = testuser\n'
                'pass = testpass\n'
                'auth_method = basic\n' % cls.port)
        os.chmod(cls.config, 0o600)

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, 'mock', None) is not None and cls.mock.poll() is None:
            cls.mock.terminate()
            try:
                cls.mock.wait(timeout=5)
            except subprocess.TimeoutExpired:
                cls.mock.kill()
                cls.mock.wait()
        if getattr(cls, 'log_handle', None) is not None:
            cls.log_handle.close()
        if getattr(cls, 'tmpdir', None):
            shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def run_cli(self, *args):
        environ = dict(os.environ)
        for name in ('AAP_DEBUG', 'RHAAP_DEBUG', 'AAP_PROFILE', 'RHAAP_PROFILE',
                     'AAP_PASSE'):
            environ.pop(name, None)
        environ['HOME'] = self.tmpdir
        environ['NO_PROXY'] = '127.0.0.1,localhost'
        environ['no_proxy'] = '127.0.0.1,localhost'
        return subprocess.run(
            CLI + ['-f', self.config, '-P', 'smoke'] + list(args),
            capture_output=True, text=True, timeout=60, env=environ)

    def test_version(self):
        proc = self.run_cli('--version')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('aap_api_cli 0.1', proc.stdout)

    def test_help_exposes_token_option(self):
        proc = self.run_cli('--help')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('--token', proc.stdout)
        self.assertNotIn('--jeton', proc.stdout)

    def test_whoami(self):
        proc = self.run_cli('whoami')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('testuser', proc.stdout)

    def test_templates_csv(self):
        proc = self.run_cli('templates', '--output', 'csv')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(proc.stdout.startswith('id,name,'), proc.stdout)
        self.assertIn('patch-linux', proc.stdout)

    def test_logs_tail(self):
        proc = self.run_cli('logs', '--job', '101', '--tail', '3')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('PLAY RECAP', proc.stdout)
        self.assertNotIn('Gathering Facts', proc.stdout)
        self.assertIn('status=successful', proc.stderr)

    def test_endpoints_json(self):
        proc = self.run_cli('endpoints', '--output', 'json')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(proc.stdout)
        self.assertEqual(data['host'], 'http://127.0.0.1:%d' % self.port)
        self.assertIn('/api/gateway/', data['api_bases'])

    def test_token_without_value_is_a_named_error(self):
        proc = self.run_cli('--auth', 'token', 'whoami')
        self.assertEqual(proc.returncode, 1)
        self.assertIn('no token configured', proc.stderr)

    def test_unreachable_host_is_concise(self):
        proc = self.run_cli('--host', 'http://127.0.0.1:1', 'status')
        self.assertEqual(proc.returncode, 1)
        lines = [line for line in proc.stderr.splitlines() if line.strip()]
        self.assertEqual(len(lines), 2, proc.stderr)
        self.assertIn('cannot reach', proc.stderr)


if __name__ == '__main__':
    unittest.main()
