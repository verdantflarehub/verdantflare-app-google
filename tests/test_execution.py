import base64
import json
from pathlib import Path
import tempfile
import unittest

from cryptography.fernet import Fernet
import httpx
from starlette.testclient import TestClient

from google_channel.execution import MODEL
from google_channel.profile import DEFAULT_ORIGIN
from google_channel.server import Google, create_execution
from google_channel.store import Store

IMAGE = base64.b64encode(b'\x89PNG\r\n\x1a\nsynthetic-test-body').decode()


class ExecutionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'state.sqlite3'
        self.key = Fernet.generate_key().decode()
        self.store = Store(str(self.path), self.key)
        self.mode = 'success'
        self.requests = []
        def upstream(request):
            self.requests.append(request)
            if self.mode == 'timeout':
                raise httpx.ReadTimeout('synthetic interruption', request=request)
            if self.mode == 'reject':
                return httpx.Response(429)
            if self.mode == 'bad':
                return httpx.Response(200, json={'candidates': 'malformed'})
            if self.mode == 'safety':
                return httpx.Response(200, json={'candidates': [{'finishReason': 'SAFETY'}]})
            return httpx.Response(200, json={'responseId': 'native-response', 'candidates': [
                {'content': {'parts': [{'inlineData': {'mimeType': 'image/png', 'data': IMAGE}}]}}]})
        self.client = httpx.Client(transport=httpx.MockTransport(upstream))
        self.google = Google(self.store, self.client)
        self.account = self.google.accounts.create('org', 'admin', 'create', {'api_url': DEFAULT_ORIGIN,
            'api_key': 'synthetic-google-key-long-enough', 'proxy': {'mode': 'direct'}, 'default': True,
            'registration': {'budget_limit': '10'}})
        self.account = self.google.accounts.checked('org', 'admin', self.account['account_id'], 1, 'active')
        self.api = TestClient(create_execution(self.google, 'e' * 40))
        self.headers = {'Authorization': 'Bearer ' + 'e' * 40, 'X-Organization-Id': 'org', 'X-Caller-Id': 'image-mcp'}

    def tearDown(self):
        self.api.close()
        self.client.close()
        self.temp.cleanup()

    def submit(self, operation='task', **changes):
        return self.api.post('/v1/calls', headers=self.headers, json={
            'idempotency_key': operation, 'task_id': operation, 'model_id': MODEL, 'capability': 'image',
            'payload': {'prompt': 'synthetic test'}, 'budget_limit': '6', **changes})

    def test_native_call_and_encrypted_result_survive_archival(self):
        result = self.submit()
        self.assertEqual(result.status_code, 200, result.text)
        value = result.json()
        self.assertEqual(value['state'], 'completed')
        self.assertTrue(value['accepted'])
        self.assertIsNone(value['settlement']['amount'])
        self.assertEqual(self.submit().json(), value)
        request = self.requests[0]
        self.assertEqual(request.url.host, 'generativelanguage.googleapis.com')
        self.assertEqual(request.url.path, '/v1beta/models/' + MODEL + ':generateContent')
        self.assertNotIn('key', request.url.params)
        self.assertNotIn('authorization', request.headers)
        self.assertEqual(json.loads(request.content)['generationConfig']['responseModalities'], ['IMAGE', 'TEXT'])
        self.google.accounts.patch('org', 'admin', self.account['account_id'], self.account['revision'], {}, archive=True)
        output = self.api.get('/v1/calls/' + value['call_id'] + '/result', headers=self.headers)
        self.assertEqual(output.json()['image']['data'], IMAGE)
        self.assertNotIn(IMAGE.encode(), self.path.read_bytes())
        self.assertEqual(len(self.requests), 1)
        other = self.api.get('/v1/calls/' + value['call_id'] + '/result', headers={**self.headers, 'X-Organization-Id': 'other'})
        self.assertEqual(other.status_code, 404)

    def test_unknown_acceptance_never_resubmits_and_keeps_reservation(self):
        self.mode = 'timeout'
        first = self.submit().json()
        self.assertIsNone(first['accepted'])
        self.assertEqual(first['state'], 'acceptance_unknown')
        self.assertEqual(self.submit().json(), first)
        self.assertEqual(self.submit('another').json()['error'], 'budget_exceeded')
        self.assertEqual(len(self.requests), 1)
        reopened = Google(Store(str(self.path), self.key), self.client)
        self.assertEqual(reopened.calls.by_operation('org', 'image-mcp', 'task'), first)

    def test_explicit_rejection_releases_reservation(self):
        self.mode = 'reject'
        self.assertFalse(self.submit().json()['accepted'])
        self.mode = 'success'
        self.assertEqual(self.submit('next').json()['state'], 'completed')

    def test_safety_result_retains_unknown_cost_and_is_not_retried(self):
        self.mode = 'safety'
        result = self.submit().json()
        self.assertEqual(result['state'], 'failed')
        self.assertTrue(result['accepted'])
        self.assertIsNone(result['settlement']['amount'])
        self.assertEqual(self.submit().json(), result)
        self.assertEqual(len(self.requests), 1)

    def test_validation_rejects_external_refs_unbudgeted_and_other_media(self):
        for change in ({'budget_limit': None}, {'capability': 'video'}, {'model_id': 'not-supported'},
                       {'payload': {'prompt': 'test', 'images': [{'url': 'https://unapproved.example/x.png'}]}},
                       {'payload': {'prompt': 'test', 'images': [{'mime_type': 'image/png', 'data': 'invalid'}]}}):
            self.assertIn(self.submit(**change).status_code, {400, 409})
        self.assertEqual(self.requests, [])
        denied = self.api.post('/v1/calls', headers={**self.headers, 'Authorization': 'Bearer ' + 'm' * 40}, json={})
        self.assertEqual(denied.status_code, 401)


if __name__ == '__main__':
    unittest.main()
