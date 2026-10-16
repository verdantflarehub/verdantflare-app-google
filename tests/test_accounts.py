import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet
import httpx
from starlette.testclient import TestClient

from google_channel.accounts import Accounts
from google_channel.profile import DEFAULT_ORIGIN
from google_channel.server import Google, create_app
from google_channel.store import Store


class GoogleAccountTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.encryption = Fernet.generate_key().decode()
        self.path = Path(self.temp.name) / 'state.sqlite3'
        self.store = Store(str(self.path), self.encryption)
        self.requests = []
        self.upstream_status = 200
        def upstream(request):
            self.requests.append(request)
            return httpx.Response(self.upstream_status, json={'models': [{'name': 'models/example'}]})
        self.google = Google(self.store, httpx.Client(transport=httpx.MockTransport(upstream)))
        self.client = TestClient(create_app(self.google, 'm' * 40))
        self.headers = {'Authorization': 'Bearer ' + 'm' * 40, 'X-Organization-Id': 'org', 'X-User-Id': 'admin'}
        self.value = {'api_url': DEFAULT_ORIGIN, 'api_key': 'synthetic-google-key-for-tests',
                      'proxy': {'mode': 'direct'}, 'default': True, 'operation_id': 'create-one'}

    def tearDown(self):
        self.client.close()
        self.google.client.close()
        self.temp.cleanup()

    def create(self, **changes):
        result = self.client.post('/v1/accounts', headers=self.headers, json={**self.value, **changes})
        self.assertEqual(result.status_code, 201, result.text)
        return result.json()

    def test_errors_survive_studio_code_only_contract(self):
        result = self.client.post('/v1/accounts', headers=self.headers, json={})
        self.assertEqual(result.status_code, 400)
        self.assertEqual(result.json()['code'], 'INVALID_ACCOUNT_REQUEST')

    def test_creation_is_encrypted_idempotent_and_project_is_optional(self):
        first = self.create()
        self.assertEqual(self.create(), first)
        self.assertEqual(first['registration'], {})
        self.assertEqual(first['name'], 'Google 账号')
        self.assertNotIn(self.value['api_key'], json.dumps(first))
        self.assertNotIn(self.value['api_key'].encode(), self.path.read_bytes())
        reopened = Accounts(Store(str(self.path), self.encryption))
        self.assertEqual(reopened.get('org', first['account_id']), first)
        self.assertEqual(self.requests, [])

    def test_google_balances_and_grants_remain_separate_after_refresh(self):
        account = self.create(registration={'billing': 'prepaid', 'balance': '12.50',
            'grant_balance': '409.45', 'grant_expiry': '2027-01-01', 'grant_note': 'eligible services only'})
        result = self.client.post('/v1/accounts/' + account['account_id'] + '/refresh', headers=self.headers)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['balance_status'], 'manual_only')
        self.assertEqual(result.json()['account']['registration'], account['registration'])
        request = self.requests[0]
        self.assertEqual(request.method, 'GET')
        self.assertEqual(request.url.path, '/v1beta/models')
        self.assertNotIn('key', request.url.params)
        self.assertEqual(request.headers['x-goog-api-key'], self.value['api_key'])
        self.assertNotIn('authorization', request.headers)

    def test_auth_and_organization_isolation(self):
        account = self.create()
        endpoint = '/v1/accounts/' + account['account_id']
        self.assertEqual(self.client.get(endpoint).status_code, 401)
        self.assertEqual(self.client.get(endpoint, headers={**self.headers, 'X-User-Id': ''}).status_code, 403)
        self.assertEqual(self.client.get(endpoint, headers={**self.headers, 'X-Organization-Id': 'other'}).status_code, 404)
        result = self.client.post('/v1/accounts/search', headers={**self.headers, 'X-Organization-Id': 'other'}, json={})
        self.assertEqual(result.json()['total'], 0)

    def test_failed_rotation_preserves_key_and_manual_registration(self):
        account = self.create(registration={'billing': 'postpaid', 'postpaid_used': '5.00'})
        self.upstream_status = 403
        result = self.client.put('/v1/accounts/' + account['account_id'] + '/credential', headers=self.headers,
            json={'api_key': 'invalid-rotation-key-for-test', 'expected_revision': account['revision']})
        self.assertEqual(result.status_code, 422)
        self.assertEqual(result.json()['account'], account)
        self.assertEqual(self.google.accounts.credential('org', account['account_id'])[1], self.value['api_key'])

    def test_disabled_default_and_archive_are_persistent(self):
        account = self.create()
        endpoint = '/v1/accounts/' + account['account_id']
        disabled = self.client.patch(endpoint, headers=self.headers, json={'status': 'disabled', 'expected_revision': 1})
        self.assertEqual(disabled.status_code, 200)
        self.assertTrue(disabled.json()['default'])
        self.assertEqual(self.client.patch(endpoint, headers=self.headers,
            json={'status': 'active', 'expected_revision': 1}).status_code, 409)
        archived = self.client.request('DELETE', endpoint, headers=self.headers, json={'expected_revision': 2})
        self.assertEqual(archived.status_code, 200)
        self.assertFalse(archived.json()['default'])
        self.assertEqual(self.client.post('/v1/accounts/search', headers=self.headers, json={}).json()['total'], 0)
        self.assertEqual(self.client.get(endpoint, headers=self.headers).json()['status'], 'archived')
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM channel_account_credentials').fetchone()[0], 1)

    def test_invalid_connections_and_supplier_specific_registration_rejected(self):
        for change in ({'api_url': 'https://unapproved.example'}, {'api_url': DEFAULT_ORIGIN + '?key=x'},
                       {'proxy': {'mode': 'proxy', 'proxy_id': 'missing'}},
                       {'registration': {'billing': 'subscription'}}, {'registration': {'membership': 'Plus'}},
                       {'registration': {'balance': '-1'}}, {'registration': {'grant_balance': 'NaN'}},
                       {'registration': {'observed_at': '2026-10-16T12:00:00'}},
                       {'registration': {'grant_expiry': 'not-a-date'}}):
            result = self.client.post('/v1/accounts', headers=self.headers, json={**self.value, **change})
            self.assertEqual(result.status_code, 400, change)
        self.assertEqual(self.requests, [])

    def test_logs_are_real_empty_storage_and_keep_org_boundary(self):
        account = self.create()
        empty = self.client.post('/v1/calls/search', headers=self.headers, json={})
        self.assertEqual(empty.status_code, 200)
        self.assertEqual(empty.json()['items'], [])
        self.assertEqual(empty.json()['summary']['task_count'], 0)
        self.google.accounts.checked('org', 'admin', account['account_id'], account['revision'], 'active')
        row, _ = self.google.calls.reserve('org', 'image-mcp', 'operation', 'task', 'image-model', {}, None, '1')
        result = self.client.post('/v1/calls/search', headers=self.headers, json={'media': 'image'})
        self.assertEqual(result.json()['items'][0]['call_id'], row['id'])
        self.assertEqual(result.json()['items'][0]['media'], 'image')
        self.assertIsNone(result.json()['summary']['budget']['amount'])
        other = self.client.post('/v1/calls/search', headers={**self.headers, 'X-Organization-Id': 'other'}, json={})
        self.assertEqual(other.json()['items'], [])
        self.assertEqual(other.json()['accounts'], [])

    def test_connection_rotation_checks_candidate_origin_and_is_atomic(self):
        account = self.create()
        url = '/v1/accounts/' + account['account_id'] + '/credential'
        candidate = 'https://approved-gateway.example'
        value = {'api_url': candidate, 'api_key': 'synthetic-new-google-key',
                 'proxy': {'mode': 'direct'}, 'expected_revision': account['revision']}
        with patch.dict('os.environ', {'GOOGLE_ALLOWED_API_ORIGINS': candidate}):
            self.upstream_status = 403
            failed = self.client.put(url, headers=self.headers, json=value)
            self.assertEqual(failed.status_code, 422)
            self.assertEqual(failed.json()['account'], account)
            self.assertEqual(self.google.accounts.credential('org', account['account_id'])[1], self.value['api_key'])
            self.upstream_status = 200
            succeeded = self.client.put(url, headers=self.headers, json=value)
            self.assertEqual(succeeded.status_code, 200)
            self.assertEqual(succeeded.json()['account']['api_url'], candidate)
            self.assertEqual(self.google.accounts.credential('org', account['account_id'])[1], value['api_key'])
        self.assertTrue(all(r.url.host == 'approved-gateway.example' for r in self.requests))
        self.assertTrue(all(r.headers['x-goog-api-key'] == value['api_key'] for r in self.requests))
        self.assertEqual(self.client.patch('/v1/accounts/' + account['account_id'], headers=self.headers,
            json={'api_url': DEFAULT_ORIGIN, 'expected_revision': 2}).status_code, 400)

    def test_existing_call_pins_connection_and_original_key(self):
        account = self.create()
        account = self.google.accounts.checked('org', 'admin', account['account_id'], account['revision'], 'active')
        row, _ = self.google.calls.reserve('org', 'image-mcp', 'before', 'task', 'image-model', {}, None, '1')
        candidate = 'https://approved-gateway.example'
        with patch.dict('os.environ', {'GOOGLE_ALLOWED_API_ORIGINS': candidate}):
            result = self.client.put('/v1/accounts/' + account['account_id'] + '/credential', headers=self.headers,
                json={'api_url': candidate, 'expected_revision': account['revision']})
        self.assertEqual(result.status_code, 200)
        pinned = self.google.calls.get('org', row['id'])
        self.assertEqual(pinned['api_url'], DEFAULT_ORIGIN)
        self.assertEqual(json.loads(pinned['proxy_json']), {'mode': 'direct'})
        self.assertEqual(self.google.calls.credential(pinned), self.value['api_key'])

    def test_concurrent_config_change_prevents_connection_commit(self):
        account = self.create()
        def during_check(candidate, key, organization=None):
            self.google.accounts.patch('org', 'another-admin', account['account_id'], account['revision'],
                                       {'status': 'disabled'})
            return 'active'
        with patch.object(self.google, 'check', side_effect=during_check):
            result = self.client.put('/v1/accounts/' + account['account_id'] + '/credential', headers=self.headers,
                json={'api_key': 'synthetic-new-google-key', 'expected_revision': account['revision']})
        self.assertEqual(result.status_code, 409)
        self.assertEqual(self.google.accounts.credential('org', account['account_id'])[1], self.value['api_key'])
        self.assertEqual(self.google.accounts.get('org', account['account_id'])['status'], 'disabled')


if __name__ == '__main__':
    unittest.main()
