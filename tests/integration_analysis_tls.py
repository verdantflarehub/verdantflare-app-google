"""Run only in the isolated 5090 Job against its delivered channel image."""
import json
import os
from pathlib import Path
import secrets
import socket
import ssl
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, HTTPSHandler, ProxyHandler

from cryptography import x509
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


def run():
    assert os.environ.get('GOOGLE_TLS_RUNTIME_CHECK') == '1', 'isolated dev runtime required'
    with tempfile.TemporaryDirectory() as root:
        root = Path(root)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'localhost')])
        now = datetime.now(timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(hours=2))
                .add_extension(x509.SubjectAlternativeName([x509.DNSName('localhost')]), critical=False)
                .sign(key, hashes.SHA256()))
        cert_file, key_file = root / 'tls.crt', root / 'tls.key'
        cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                             serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        key_file.chmod(0o600)
        analysis_token, execution_token = secrets.token_hex(32), secrets.token_hex(32)
        env = {'PATH': os.environ['PATH'], 'PYTHONPATH': '/app', 'PYTHONDONTWRITEBYTECODE': '1',
               'GOOGLE_STATE_DB': str(root / 'state.sqlite3'),
               'GOOGLE_ENCRYPTION_KEY': Fernet.generate_key().decode(),
               'GOOGLE_ANALYSIS_TOKEN': analysis_token, 'GOOGLE_EXECUTION_TOKEN': execution_token,
               'GOOGLE_ANALYSIS_ENABLED': 'false',
               'GOOGLE_ANALYSIS_TLS_CERT_FILE': str(cert_file),
               'GOOGLE_ANALYSIS_TLS_KEY_FILE': str(key_file)}
        trusted = ssl.create_default_context(cafile=str(cert_file))
        plain = build_opener(ProxyHandler({}))
        secure = build_opener(ProxyHandler({}), HTTPSHandler(context=trusted))
        headers = {'Authorization': 'Bearer ' + analysis_token,
                   'X-Organization-Id': 'tls-runtime-test', 'X-Caller-Id': 'world-analysis'}
        checks = []

        def request(opener, url, *, supplied=headers, data=None):
            req = Request(url, data=data, headers={**supplied, 'Content-Type': 'application/json'})
            try:
                response = opener.open(req, timeout=3)
            except HTTPError as e:
                response = e
            with response:
                return response.status, json.load(response)

        def start(settings):
            proc = subprocess.Popen([sys.executable, '-m', 'google_channel.server'], env=settings,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for _ in range(100):
                if proc.poll() is not None:
                    raise AssertionError('channel exited before readiness')
                try:
                    status, _ = request(plain, 'http://127.0.0.1:8000/health', supplied={})
                    if status == 200:
                        return proc
                except (OSError, URLError):
                    pass
                time.sleep(0.1)
            stop(proc)
            raise AssertionError('channel readiness timeout')

        def stop(proc):
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)

        proc = start(env)
        try:
            for opener, base in [(plain, 'http://127.0.0.1:8001'), (secure, 'https://localhost:8002')]:
                status, body = request(opener, base + '/v1/analysis/capabilities')
                assert status == 200 and body['execution_state'] == 'disabled'
            checks.append('legacy HTTP and verified HTTPS capabilities')
            for context, hostname in [(ssl.create_default_context(), 'localhost'), (trusted, 'wrong-host.test')]:
                try:
                    with socket.create_connection(('127.0.0.1', 8002), timeout=3) as conn:
                        with context.wrap_socket(conn, server_hostname=hostname):
                            pass
                except ssl.SSLCertVerificationError:
                    continue
                raise AssertionError('untrusted certificate or wrong hostname accepted')
            checks.append('untrusted certificate and hostname mismatch rejected')
            for caller, token in [('image-mcp', execution_token), ('video-mcp', execution_token),
                                  ('image-mcp', analysis_token), ('world-analysis', execution_token)]:
                status, _ = request(secure, 'https://localhost:8002/v1/analysis/capabilities',
                                    supplied={**headers, 'X-Caller-Id': caller, 'Authorization': 'Bearer ' + token})
                assert status == 401
            checks.append('non-analysis credentials and caller substitution rejected')
            status, body = request(secure, 'https://localhost:8002/v1/calls', data=b'{}')
            assert status == 503 and body['accepted'] is False
            status, _ = request(secure, 'https://localhost:8002/v1/calls/by-idempotency/missing-operation')
            assert status == 404
            checks.append('disabled admission rejects POST but permits scoped lookup')
        finally:
            stop(proc)
        no_tls = {k: v for k, v in env.items() if not k.startswith('GOOGLE_ANALYSIS_TLS_')}
        proc = start(no_tls)
        try:
            try:
                with socket.create_connection(('127.0.0.1', 8002), timeout=1):
                    raise AssertionError('optional TLS listener opened without configuration')
            except ConnectionRefusedError:
                pass
            checks.append('no TLS listener when certificate configuration absent')
        finally:
            stop(proc)
        key_file.write_bytes(b'invalid key')
        invalid = subprocess.run([sys.executable, '-m', 'google_channel.server'], env=env,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
        assert invalid.returncode != 0
        checks.append('invalid certificate configuration fails startup')
        print(json.dumps({'passed': True, 'checks': checks,
                          'scope': 'actual image TLS and authorization; no provider inference or user installation'}, indent=2))


if __name__ == '__main__':
    run()
