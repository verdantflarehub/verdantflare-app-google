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

    def test_draining_rejects_new_submission_but_allows_original_call_read(self):
        original = self.submit().json()
        count = len(self.requests)
        self.google.accepting = False
        rejected = self.submit('another-task')
        self.assertEqual(rejected.status_code, 503)
        self.assertIs(rejected.json()['accepted'], False)
        self.assertEqual(len(self.requests), count)
        result = self.api.get('/v1/calls/' + original['call_id'], headers=self.headers)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['call_id'], original['call_id'])

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

    def test_video_submission_polling_and_encrypted_result(self):
        video_headers = {**self.headers, 'X-Caller-Id': 'video-mcp'}
        def video_upstream(request):
            self.requests.append(request)
            if request.method == 'POST' and request.url.path.endswith(':predictLongRunning'):
                return httpx.Response(200, json={'name': 'models/veo-3.1-lite-generate-preview/operations/op-123'})
            if request.method == 'GET' and request.url.path == '/v1beta/models/veo-3.1-lite-generate-preview/operations/op-123':
                return httpx.Response(200, json={'name': 'models/veo-3.1-lite-generate-preview/operations/op-123',
                    'done': True, 'response': {'generateVideoResponse': {'generatedSamples': [
                        {'video': {'uri': 'https://generativelanguage.googleapis.com/v1beta/files/f123:download?alt=media'}}]}}})
            if request.method == 'GET' and request.url.path == '/v1beta/files/f123:download':
                self.assertIn('x-goog-api-key', request.headers)
                return httpx.Response(200, content=b'\x00\x00\x00\x20ftypisom-veo-data',
                    headers={'content-type': 'video/mp4'})
            return httpx.Response(404)
        self.google.client = self.google.execution.client = httpx.Client(transport=httpx.MockTransport(video_upstream))
        self.addCleanup(self.google.client.close)
        submitted = self.api.post('/v1/calls', headers=video_headers, json={
            'idempotency_key': 'veo-task', 'task_id': 'veo-task', 'model_id': 'veo-3.1-lite-generate-preview',
            'capability': 'video', 'payload': {'prompt': 'veo test', 'duration': 4, 'resolution': '720p', 'aspect_ratio': '16:9'},
            'budget_limit': '2'})
        self.assertEqual(submitted.status_code, 200, submitted.text)
        call = submitted.json()
        self.assertEqual(call['state'], 'in_progress')
        polled = self.api.get('/v1/calls/' + call['call_id'], headers=video_headers).json()
        self.assertEqual(polled['state'], 'completed')
        content = self.api.get('/v1/calls/' + call['call_id'] + '/result', headers=video_headers)
        self.assertEqual(content.status_code, 200)
        self.assertEqual(content.content, b'\x00\x00\x00\x20ftypisom-veo-data')
        self.assertEqual(self.api.get('/v1/calls/' + call['call_id'] + '/result', headers=self.headers).status_code, 403)

    def test_image_aspect_ratio_forwarding_and_2_3_fallback(self):
        res_916 = self.submit('task-916', payload={'prompt': 'vertical portrait', 'aspect_ratio': '9:16'}, budget_limit='2')
        self.assertEqual(res_916.status_code, 200, res_916.text)
        body_916 = json.loads(self.requests[-1].content)
        self.assertEqual(body_916['generationConfig']['imageConfig'], {'aspectRatio': '9:16'})

        res_23 = self.submit('task-23', payload={'prompt': 'editorial portrait', 'aspect_ratio': '2:3'}, budget_limit='2')
        self.assertEqual(res_23.status_code, 200, res_23.text)
        body_23 = json.loads(self.requests[-1].content)
        self.assertEqual(body_23['generationConfig']['imageConfig'], {'aspectRatio': '3:4'})
        self.assertIn('[Vertical Portrait 2:3 / 3:4 Aspect Ratio]', body_23['contents'][0]['parts'][-1]['text'])

        bad = self.submit('task-bad-ar', payload={'prompt': 'bad ar', 'aspect_ratio': 'invalid-ratio'}, budget_limit='2')
        self.assertEqual(bad.status_code, 400)


if __name__ == '__main__':
    unittest.main()
