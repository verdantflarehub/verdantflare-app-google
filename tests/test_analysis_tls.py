"""No listeners: certificate parsing and analysis-only ASGI authorization."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import ssl
import tempfile
import unittest

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from starlette.testclient import TestClient

from google_channel.analysis_tls import analysis_tls_options
from google_channel.server import create_execution


class AnalysisTLSTest(unittest.TestCase):
    def test_absent_incomplete_or_invalid_configuration(self):
        self.assertIsNone(analysis_tls_options({}))
        for env in ({'GOOGLE_ANALYSIS_TLS_CERT_FILE': 'missing'},
                    {'GOOGLE_ANALYSIS_TLS_KEY_FILE': 'missing'},
                    {'GOOGLE_ANALYSIS_TLS_CERT_FILE': 'missing',
                     'GOOGLE_ANALYSIS_TLS_KEY_FILE': 'missing',
                     'GOOGLE_ANALYSIS_TOKEN': 'a' * 40}):
            with self.assertRaisesRegex(ValueError, '^invalid_analysis_tls_configuration$'):
                analysis_tls_options(env)

    def test_matching_certificate_required_and_tls_server_options(self):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'analysis.test')])
        now = datetime.now(timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(minutes=1))
                .not_valid_after(now + timedelta(days=1)).sign(key, hashes.SHA256()))
        with tempfile.TemporaryDirectory() as root:
            c, k = Path(root) / 'tls.crt', Path(root) / 'tls.key'
            c.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
            k.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                          serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
            env = {'GOOGLE_ANALYSIS_TLS_CERT_FILE': str(c), 'GOOGLE_ANALYSIS_TLS_KEY_FILE': str(k),
                   'GOOGLE_ANALYSIS_TOKEN': 'a' * 40}
            opts = analysis_tls_options(env)
            self.assertEqual(opts['ssl_version'], ssl.PROTOCOL_TLS_SERVER)
            self.assertEqual(opts['ssl_certfile'], str(c))
            context = ssl.SSLContext(opts['ssl_version'])
            self.assertGreaterEqual(context.minimum_version, ssl.TLSVersion.TLSv1_2)
            k.write_bytes(b'invalid key')
            with self.assertRaisesRegex(ValueError, '^invalid_analysis_tls_configuration$'):
                analysis_tls_options(env)

    def test_analysis_only_identity_and_disabled_admission_recovery(self):
        class Calls:
            def by_operation(self, org, caller, operation):
                return {'org': org, 'caller': caller, 'operation': operation}
        class Service:
            accepting = True
            calls = Calls()
        headers = {'Authorization': 'Bearer ' + 'a' * 40,
                   'X-Caller-Id': 'world-analysis', 'X-Organization-Id': 'test-org'}
        with TestClient(create_execution(Service(), '', analysis_token='a' * 40,
                        analysis_enabled=False, analysis_catalog=[])) as client:
            result = client.get('/v1/analysis/capabilities', headers=headers)
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json()['execution_state'], 'disabled')
            self.assertEqual(client.post('/v1/calls', headers=headers, json={}).status_code, 503)
            result = client.get('/v1/calls/by-idempotency/original-operation', headers=headers)
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json()['caller'], 'world-analysis')
            for caller in ('image-mcp', 'video-mcp'):
                self.assertEqual(client.get('/v1/analysis/capabilities',
                    headers={**headers, 'X-Caller-Id': caller}).status_code, 401)
            self.assertEqual(client.get('/v1/accounts', headers=headers).status_code, 404)
