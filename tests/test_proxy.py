import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet
import httpx
from starlette.testclient import TestClient
from google_channel.proxy import NetworkProxies, ProxyUnavailable
from google_channel.server import Google, create_app
from google_channel.store import Store, Conflict
from google_channel.profile import DEFAULT_ORIGIN
from google_channel.execution import MODEL

ORG = '0199b501-0000-7000-8000-000000000001'
PID = '0199b501-0000-7000-8000-000000000002'
REF = {'mode': 'proxy', 'proxy_id': PID, 'revision': 4}


class NetworkProxyTest(unittest.TestCase):
    def test_resolution_is_bound_and_does_not_follow_redirects(self):
        sent = []
        response = {'schema_version': 1, 'proxy_id': PID, 'revision': 4, 'protocol': 'socks5h',
                    'host': 'proxy.example.invalid', 'port': 1080, 'username': 'fixture@user', 'password': 'a/b:c'}
        def upstream(request):
            sent.append(request)
            return httpx.Response(200, json=response)
        with httpx.Client(transport=httpx.MockTransport(upstream), follow_redirects=False) as client:
            network = NetworkProxies('http://network.example.invalid', 't' * 40, client=client)
            self.assertEqual(network.resolve_url(ORG, REF), 'socks5h://fixture%40user:a%2Fb%3Ac@proxy.example.invalid:1080')
            self.assertEqual(sent[0].headers['x-organization-id'], ORG)
            self.assertEqual(json.loads(sent[0].content), {'schema_version': 1, 'proxy_id': PID, 'revision': 4})
            response['revision'] = 5
            with self.assertRaises(ProxyUnavailable):
                network.resolve_url(ORG, REF)
            response['revision'] = 4
            response['host'] = 'proxy.example.invalid@attacker.invalid'
            with self.assertRaises(ProxyUnavailable):
                network.resolve_url(ORG, REF)
        redirects = []
        def redirect(request):
            redirects.append(str(request.url))
            return httpx.Response(307, headers={'location': 'http://attacker.invalid/'})
        with httpx.Client(transport=httpx.MockTransport(redirect), follow_redirects=False) as client:
            network = NetworkProxies('http://network.example.invalid', 't' * 40, client=client)
            with self.assertRaises(ProxyUnavailable):
                network.resolve_url(ORG, REF)
        self.assertEqual(len(redirects), 1)

    def test_list_rejects_secret_fields_and_duplicate_json(self):
        for content in [json.dumps({'schema_version': 1, 'items': [{'proxy_id': PID, 'name': 'Proxy',
                'protocol': 'http', 'status': 'active', 'revision': 4, 'expires_at': None, 'password': 'synthetic'}]}),
                '{"schema_version":1,"schema_version":1,"items":[]}']:
            with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=content))) as client:
                network = NetworkProxies('http://network.example.invalid', 't' * 40, client=client)
                with self.assertRaises(ProxyUnavailable):
                    network.list(ORG)


class FakeNetwork:
    def __init__(self):
        self.seen = []
        self.fail = False

    def resolve_url(self, org, value):
        self.seen.append((org, dict(value)))
        if self.fail:
            raise ProxyUnavailable()
        return 'http://proxy.example.invalid:1080'

    def list(self, org):
        return {'schema_version': 1, 'items': [{'proxy_id': PID, 'name': 'Proxy', 'protocol': 'http',
            'revision': 4, 'status': 'active', 'expires_at': None}]}

    def close(self):
        pass


class GoogleProxyTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(str(Path(self.temp.name) / 'state.sqlite3'), Fernet.generate_key().decode())
        self.network = FakeNetwork()
        self.direct = []
        def direct(request):
            self.direct.append(request.method)
            raise AssertionError('proxy call fell back to direct')
        self.google = Google(self.store, httpx.Client(transport=httpx.MockTransport(direct)), network=self.network)
        self.api = TestClient(create_app(self.google, 'm' * 40))
        self.headers = {'Authorization': 'Bearer ' + 'm' * 40, 'X-Organization-Id': ORG, 'X-User-Id': 'admin'}
        self.value = {'api_url': DEFAULT_ORIGIN, 'api_key': 'synthetic-google-key-long-enough',
                      'proxy': REF, 'default': True, 'registration': {'budget_limit': '10'}}
        self.account = self.google.accounts.create(ORG, 'admin', 'create', self.value,
            validate_proxy=lambda value: self.google.validate_proxy(ORG, value))
        self.account = self.google.accounts.checked(ORG, 'admin', self.account['account_id'], 1, 'active')
        self.real_client = httpx.Client
        self.sent = []
        self.timeout = False

    def tearDown(self):
        self.api.close()
        self.google.client.close()
        self.temp.cleanup()

    def proxy_client(self, **kwargs):
        self.assertFalse(kwargs['trust_env'])
        self.assertFalse(kwargs['follow_redirects'])
        self.assertEqual(kwargs['proxy'], 'http://proxy.example.invalid:1080')
        def upstream(request):
            self.sent.append(request)
            if self.timeout:
                raise httpx.ReadTimeout('synthetic')
            return httpx.Response(200, json={'models': []})
        return self.real_client(transport=httpx.MockTransport(upstream))

    def submit(self, operation='task'):
        return self.google.execution.submit(ORG, 'image-mcp', operation, operation,
            MODEL, {'prompt': 'synthetic test'}, self.account['account_id'], '6')

    def test_unavailable_proxy_releases_budget_without_direct_fallback(self):
        self.network.fail = True
        with self.assertRaisesRegex(Conflict, 'proxy_unavailable'):
            self.submit()
        self.assertEqual(self.google.calls.by_operation(ORG, 'image-mcp', 'task')['accepted'], False)
        self.network.fail = False
        self.timeout = True
        with patch('google_channel.server.httpx.Client', side_effect=self.proxy_client):
            first = self.submit('next')
            self.assertEqual(first['state'], 'acceptance_unknown')
            self.assertEqual(self.submit('next'), first)
            with self.assertRaisesRegex(Conflict, 'budget_exceeded'):
                self.submit('third')
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.direct, [])

    def test_create_replay_does_not_depend_on_current_proxy_availability(self):
        self.network.fail = True
        replay = self.google.accounts.create(ORG, 'admin', 'create', self.value,
            validate_proxy=lambda value: self.google.validate_proxy(ORG, value))
        self.assertEqual(replay['account_id'], self.account['account_id'])
        failed = self.api.post('/v1/accounts', headers=self.headers,
            json={**self.value, 'operation_id': 'different'})
        self.assertEqual(failed.status_code, 503)
        self.assertEqual(self.google.accounts.list(ORG)['total'], 1)

    def test_rotation_failure_preserves_key_proxy_and_revision(self):
        self.network.fail = True
        result = self.api.put('/v1/accounts/' + self.account['account_id'] + '/credential',
            headers=self.headers, json={'expected_revision': self.account['revision'],
                'proxy': REF, 'api_key': 'synthetic-replacement-key'})
        self.assertEqual(result.status_code, 503)
        self.assertEqual(self.google.accounts.get(ORG, self.account['account_id']), self.account)
        self.assertEqual(self.google.accounts.credential(ORG, self.account['account_id'])[1], self.value['api_key'])
        self.assertEqual(self.direct, [])

    def test_refresh_and_execution_use_pinned_org_proxy_and_key(self):
        with patch('google_channel.server.httpx.Client', side_effect=self.proxy_client):
            refreshed = self.api.post('/v1/accounts/' + self.account['account_id'] + '/refresh', headers=self.headers)
            self.assertEqual(refreshed.status_code, 200)
            first = self.submit()
        row = self.google.calls.get(ORG, first['call_id'])
        current = refreshed.json()['account']
        self.google.accounts.checked(ORG, 'admin', current['account_id'], current['revision'], 'active',
            'synthetic-replacement-key', {'api_url': DEFAULT_ORIGIN, 'proxy': {'mode': 'direct'}})
        self.assertEqual(json.loads(row['proxy_json']), REF)
        self.assertEqual(self.google.calls.credential(row), self.value['api_key'])
        self.assertEqual(self.submit(), first)
        self.assertEqual([r.method for r in self.sent], ['GET', 'POST'])
        self.assertTrue(all(r.headers['x-goog-api-key'] == self.value['api_key'] for r in self.sent))
        self.assertTrue(all(pair == (ORG, REF) for pair in self.network.seen))
        self.assertEqual(self.direct, [])
