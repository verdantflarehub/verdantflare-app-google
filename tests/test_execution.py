import base64
import json
from pathlib import Path
import tempfile
import unittest

from cryptography.fernet import Fernet
import httpx
from starlette.testclient import TestClient

from google_channel.execution import MODEL, video_payload
from google_channel.metadata import image_capabilities, video_capabilities
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
            if self.mode == 'text_only_stop':
                return httpx.Response(200, json={'responseId': 'resp-stop', 'candidates': [
                    {'finishReason': 'STOP', 'content': {'parts': [{'text': 'I cannot generate this image.'}]}}]})
            if self.mode == 'prompt_blocked':
                return httpx.Response(200, json={'responseId': 'resp-block', 'promptFeedback': {'blockReason': 'PROHIBITED_CONTENT'}})
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
        self.assertEqual(MODEL, 'gemini-nano-banana-2.1')
        result = self.submit()
        self.assertEqual(result.status_code, 200, result.text)
        value = result.json()
        self.assertEqual(value['state'], 'completed')
        self.assertTrue(value['accepted'])
        self.assertEqual(value['settlement']['amount'], '0.0336')
        self.assertEqual(value['settlement']['source'], 'price_table')
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
        self.assertEqual(result['reason_code'], 'safety')
        self.assertIsNone(result['settlement']['amount'])
        self.assertEqual(self.submit().json(), result)
        self.assertEqual(len(self.requests), 1)

        self.mode = 'text_only_stop'
        stop_res = self.submit('task-stop', budget_limit='1').json()
        self.assertEqual(stop_res['state'], 'failed')
        self.assertTrue(stop_res['accepted'])
        self.assertEqual(stop_res['reason_code'], 'no_image_returned')

        self.mode = 'prompt_blocked'
        block_res = self.submit('task-block', budget_limit='1').json()
        self.assertEqual(block_res['state'], 'failed')
        self.assertTrue(block_res['accepted'])
        self.assertEqual(block_res['reason_code'], 'prohibited_content')

    def test_validation_rejects_external_refs_unbudgeted_and_other_media(self):
        for change in ({'budget_limit': None}, {'capability': 'video'}, {'model_id': 'not-supported'},
                       {'model_id': 'imagen-4-ultra'},
                       {'payload': {'prompt': 'test', 'images': [{'url': 'https://unapproved.example/x.png'}]}},
                       {'payload': {'prompt': 'test', 'images': [{'mime_type': 'image/png', 'data': 'invalid'}]}}):
            self.assertIn(self.submit(**change).status_code, {400, 409})
        self.assertEqual(self.requests, [])
        expected_settlements = {
            'gemini-nano-banana-2.1': ('gemini-nano-banana-2.1', '0.0336', 'price_table'),
            'gemini-3.1-flash-lite-image': ('gemini-3.1-flash-lite-image', '0.0336', 'price_table'),
            'gemini-3.1-flash-image': ('gemini-3.1-flash-image', '0.0672', 'price_table'),
            'gemini-3-pro-image': ('gemini-3-pro-image', '0.134', 'price_table'),
            'gemini-2.5-flash-image': ('gemini-2.5-flash-image', None, 'unknown'),
            'gemini-3-pro-image-preview': ('gemini-3-pro-image', '0.134', 'price_table'),
            'gemini-3.1-flash-image-preview': ('gemini-3.1-flash-image', '0.0672', 'price_table'),
        }
        for valid_model, (canonical_model, expected_amount, expected_source) in expected_settlements.items():
            res = self.submit(f'valid-{valid_model}', model_id=valid_model, budget_limit='1')
            self.assertEqual(res.status_code, 200, res.text)
            body = res.json()
            self.assertEqual(body['model_id'], canonical_model)
            self.assertEqual(body['settlement']['amount'], expected_amount)
            self.assertEqual(body['settlement']['source'], expected_source)
            self.assertEqual(self.requests[-1].url.path, f'/v1beta/models/{canonical_model}:generateContent')
        self.requests.clear()
        denied = self.api.post('/v1/calls', headers={**self.headers, 'Authorization': 'Bearer ' + 'm' * 40}, json={})
        self.assertEqual(denied.status_code, 401)

    def test_mcp_capabilities_endpoint_and_chart_yaml_validation(self):
        chart_yaml = Path(__file__).resolve().parents[2] / 'verdantflare-market' / 'charts' / 'google' / 'channel.yaml'
        if chart_yaml.is_file():
            loaded = image_capabilities(chart_yaml)
            self.assertEqual(len(loaded), 8)
            self.assertEqual(
                [item['model_id'] for item in loaded if item['operation'] == 'image.generate'],
                ['gemini-nano-banana-2.1', 'gemini-3-pro-image', 'gemini-3.1-flash-image', 'gemini-3.1-flash-lite-image'],
            )
            self.assertEqual(
                [item['display_name'] for item in loaded if item['operation'] == 'image.generate'],
                ['Nano Banana 2.1', 'Nano Banana Pro', 'Nano Banana 2', 'Nano Banana 2 Lite'],
            )
        cap_res = self.api.get('/v1/capabilities', headers=self.headers)
        self.assertEqual(cap_res.status_code, 200, cap_res.text)
        cap_body = cap_res.json()
        self.assertEqual(cap_body['schema_version'], 1)
        self.assertEqual(cap_body['channel_id'], 'google')
        self.assertEqual(cap_body['default_model_id'], 'gemini-nano-banana-2.1')
        self.assertEqual(cap_body['execution_state'], 'enabled')
        self.assertEqual(
            cap_body['models'],
            ['gemini-nano-banana-2.1', 'gemini-3-pro-image', 'gemini-3.1-flash-image', 'gemini-3.1-flash-lite-image'],
        )
        self.assertEqual(len(cap_body['capabilities']), 8)
        self.google.accepting = False
        draining_cap = self.api.get('/v1/capabilities', headers=self.headers).json()
        self.assertEqual(draining_cap['execution_state'], 'disabled')
        self.assertTrue(all(item['execution_state'] == 'disabled' for item in draining_cap['capabilities']))

    def test_retired_video_preserves_original_submission_polling_and_encrypted_result(self):
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
        original_payload = {'prompt': 'veo test', 'duration': 4, 'resolution': '720p', 'aspect_ratio': '16:9'}
        # Existing intent was created before the provider's shutdown, not by a new API call.
        legacy, _ = self.google.calls.reserve('org', 'video-mcp', 'veo-task', 'veo-task',
            'veo-3.1-lite-generate-preview', video_payload(original_payload), None, '2', media='video')
        self.google.calls.accepted(legacy['id'], 'models/veo-3.1-lite-generate-preview/operations/op-123')
        self.google.calls.observed(legacy['id'], 'in_progress')
        submitted = self.api.post('/v1/calls', headers=video_headers, json={
            'idempotency_key': 'veo-task', 'task_id': 'veo-task', 'model_id': 'veo-3.1-lite-generate-preview',
            'capability': 'video', 'payload': original_payload,
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
        self.assertFalse(any(request.method == 'POST' for request in self.requests))

    def test_retired_video_preflight_and_submit_never_reserve_or_call_upstream(self):
        video_headers = {**self.headers, 'X-Caller-Id': 'video-mcp'}
        for model in ('veo-3.1-lite-generate-preview', 'veo-3.1-fast-generate-preview',
                      'veo-3.1-generate-preview'):
            with self.subTest(model=model):
                quote = self.api.post('/v1/video-preflight', headers=video_headers, json={
                    'model_id': model, 'payload': {'prompt': 'test'}, 'budget_limit': '2'})
                self.assertEqual(quote.status_code, 200)
                self.assertFalse(quote.json()['can_submit'])
                self.assertEqual(quote.json()['blocked_reason'], 'model_retired')
                self.assertIsNone(quote.json()['quote']['amount'])
                self.assertEqual(quote.json()['model_status']['retired_on'], '2026-10-22')
                rejected = self.api.post('/v1/calls', headers=video_headers, json={
                    'idempotency_key': model, 'task_id': model, 'model_id': model, 'capability': 'video',
                    'payload': {'prompt': 'test', 'duration': 5, 'resolution': '480p'}, 'budget_limit': '2'})
                self.assertEqual(rejected.status_code, 409)
                self.assertEqual(rejected.json()['error'], 'model_retired')
        self.assertEqual(self.requests, [])
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM channel_calls').fetchone()[0], 0)
        self.assertEqual(self.api.post('/v1/video-preflight', headers=self.headers,
            json={'model_id': 'veo-3.1-lite-generate-preview', 'payload': {}}).status_code, 403)

    def test_video_catalog_uses_installed_declarations_and_retirement(self):
        declared = {'schema_version': 1, 'channel_id': 'google', 'capabilities': [{
            'capability_id': 'video.generate.veo_lite', 'type': 'video',
            'model_id': 'veo-3.1-lite-generate-preview', 'display_name': 'Veo 3.1 Lite',
            'operation': 'video.generate', 'execution_state': 'enabled', 'parameter_schema': {}}]}
        metadata_path = Path(self.temp.name) / 'channel.json'
        metadata_path.write_text(json.dumps(declared), encoding='utf-8')
        catalog = video_capabilities(metadata_path)
        self.assertEqual(catalog[0]['execution_state'], 'disabled')
        self.assertEqual(catalog[0]['reason_code'], 'model_retired')
        with TestClient(create_execution(self.google, 'e' * 40, video_catalog=catalog)) as api:
            response = api.get('/v1/capabilities', headers={**self.headers, 'X-Caller-Id': 'video-mcp'}).json()
        self.assertEqual(response['execution_state'], 'disabled')
        self.assertEqual(response['models'], [])
        self.assertEqual(response['capabilities'], catalog)
        self.assertIsNone(response['default_model_id'])
        self.assertEqual(video_capabilities(None), [])

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
