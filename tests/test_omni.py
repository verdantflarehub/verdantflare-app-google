import base64
from datetime import date
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet
import httpx
from starlette.testclient import TestClient

from google_channel.execution import image
from google_channel.metadata import video_capabilities
from google_channel.omni import payload
from google_channel.profile import DEFAULT_ORIGIN
from google_channel.server import Google, create_execution
from google_channel.store import Conflict, Store
from google_channel.video_pricing import OMNI_MODEL, PRICE_VERSION, estimate, settlement

VIDEO = b'\x00\x00\x00\x20ftypisom-omni-test'
IMAGE = {'mime_type': 'image/png', 'data': base64.b64encode(b'\x89PNG\r\n\x1a\nsynthetic').decode()}
USAGE = {'total_input_tokens': 100, 'total_output_tokens': 23178, 'total_thought_tokens': 20,
         'total_tokens': 23298, 'output_tokens_by_modality': [
             {'modality': 'video', 'tokens': 23168}, {'modality': 'text', 'tokens': 10}]}
CATALOG = [{'capability_id': 'video.generate.omni', 'type': 'video', 'model_id': OMNI_MODEL,
            'operation': 'video.generate', 'display_name': 'Omni 1.1 Flash',
            'execution_state': 'enabled', 'parameter_schema': {'type': 'object'}}]


class PricingTest(unittest.TestCase):
    def test_partial_quote_and_expiry_never_turn_ceiling_into_total(self):
        value = estimate({'duration': '4s', 'resolution': '720p'}, today=date(2026, 10, 27))
        self.assertIsNone(value['amount'])
        self.assertEqual(value['known_amount'], '0.40544')
        self.assertEqual(value['price_version'], PRICE_VERSION)
        self.assertEqual(value['unknown_components'], ['input_tokens', 'text_output_tokens', 'thought_tokens'])
        with self.assertRaisesRegex(Conflict, 'price_table_expired'):
            estimate({'duration': '4s', 'resolution': '720p'}, today=date(2026, 11, 27))
        with self.assertRaisesRegex(Conflict, 'price_unknown'):
            estimate({'duration': '4s', 'resolution': '1080p'}, today=date(2026, 10, 27))

    def test_settlement_requires_complete_consistent_reported_usage(self):
        bill, usage = settlement(USAGE, PRICE_VERSION)
        self.assertEqual(bill['amount'], '0.40586')
        self.assertEqual(bill['source'], 'price_table')
        self.assertEqual(usage, USAGE)
        for invalid in ({}, {**USAGE, 'total_thought_tokens': None}, {**USAGE, 'total_tokens': 1},
                        {**USAGE, 'total_cached_tokens': 2}, {**USAGE, 'total_tool_use_tokens': 2},
                        {**USAGE, 'output_tokens_by_modality': [{'modality': 'video', 'tokens': 23178},
                                                               {'modality': 'video', 'tokens': 0}]},
                        {**USAGE, 'output_tokens_by_modality': [{'modality': 'audio', 'tokens': 23178}]}):
            self.assertIsNone(settlement(invalid, PRICE_VERSION)[0]['amount'])
        self.assertEqual(settlement(USAGE, 'old-price')[0]['reason_code'], 'price_version_unknown')

    def test_parameters_are_strict_and_seconds_are_preserved(self):
        request = {'prompt': 'test', 'duration_seconds': 5}
        self.assertEqual(payload(request, image)['duration'], '5s')
        for extra in ({'duration_seconds': 2}, {'duration_seconds': 11}, {'duration_seconds': True},
                      {'duration': '5s'}, {'resolution': '480p'}, {'aspect_ratio': '1:1'},
                      {'image': IMAGE}, {'mode': 'image_to_video'}, {'videos': []}, {'images': [IMAGE]}):
            with self.assertRaises((Conflict, ValueError)):
                payload({**request, **extra}, image)
        self.assertEqual(payload({**request, 'mode': 'image_to_video', 'image': IMAGE}, image)['image'], IMAGE)


class OmniTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.requests = []
        self.mode = 'inline'
        self.ready = False
        def upstream(request):
            self.requests.append(request)
            if self.mode == 'timeout':
                raise httpx.ReadTimeout('synthetic', request=request)
            if self.mode == 'empty_reject':
                return httpx.Response(429)
            if self.mode == 'reject':
                return httpx.Response(400, json={'error': {'status': 'INVALID_ARGUMENT',
                    'message': 'duration is invalid; token=synthetic-secret https://unsafe.example/private'}})
            if request.url.path.startswith('/v1beta/files/'):
                if request.url.path.endswith(':download'):
                    return httpx.Response(200, content=VIDEO, headers={'content-type': 'video/mp4'})
                return httpx.Response(200, json={'state': 'ACTIVE' if self.ready else 'PROCESSING'})
            if request.method == 'POST':
                return httpx.Response(200, json={'id': 'interaction_1', 'model': OMNI_MODEL, 'status': 'in_progress'})
            if self.mode == 'failed':
                return httpx.Response(200, json={'id': 'interaction_1', 'model': OMNI_MODEL, 'status': 'failed',
                    'error': {'status': 'RESOURCE_EXHAUSTED', 'message': 'Video quota exceeded.'}})
            video = {'type': 'video', 'mime_type': 'video/mp4'}
            if self.mode == 'uri':
                video['uri'] = DEFAULT_ORIGIN + '/v1beta/files/file_1'
            else:
                video['data'] = base64.b64encode(VIDEO).decode()
            return httpx.Response(200, json={'id': 'interaction_1', 'model': OMNI_MODEL, 'status': 'completed',
                'steps': [{'type': 'model_output', 'content': [video]}], 'usage': USAGE})
        self.client = httpx.Client(transport=httpx.MockTransport(upstream))
        self.addCleanup(self.client.close)
        self.google = Google(Store(str(Path(self.temp.name) / 'state.sqlite3'), Fernet.generate_key().decode()), self.client)
        created = self.google.accounts.create('org', 'admin', 'create', {'api_url': DEFAULT_ORIGIN,
            'api_key': 'synthetic-google-key-long-enough', 'proxy': {'mode': 'direct'}, 'default': True,
            'registration': {'budget_limit': '10'}})
        self.google.accounts.checked('org', 'admin', created['account_id'], 1, 'active')
        self.api = TestClient(create_execution(self.google, 'e' * 40, video_catalog=CATALOG))
        self.addCleanup(self.api.close)
        self.headers = {'Authorization': 'Bearer ' + 'e' * 40, 'X-Organization-Id': 'org', 'X-Caller-Id': 'video-mcp'}
        price_patch = patch('google_channel.omni.estimate', side_effect=lambda value: estimate(value, today=date(2026, 10, 27)))
        price_patch.start()
        self.addCleanup(price_patch.stop)

    def submit(self, operation='omni-task', **changes):
        return self.api.post('/v1/calls', headers=self.headers, json={
            'idempotency_key': operation, 'task_id': operation, 'model_id': OMNI_MODEL, 'capability': 'video',
            'payload': {'prompt': 'test', 'duration_seconds': 4}, 'budget_limit': '2', **changes})

    def poll(self, call):
        return self.api.get('/v1/calls/' + call['call_id'], headers=self.headers).json()

    def test_capabilities_use_service_identity_while_business_calls_require_organization(self):
        headers = {k: v for k, v in self.headers.items() if k != 'X-Organization-Id'}
        listing = self.api.get('/v1/capabilities', headers=headers)
        self.assertEqual(listing.status_code, 200)
        self.assertIn(OMNI_MODEL, listing.json()['models'])
        self.assertEqual(self.api.post('/v1/video-preflight', headers=headers, json={}).status_code, 403)
        self.assertEqual(self.api.get('/v1/capabilities', headers={**headers,
            'Authorization': 'Bearer invalid'}).status_code, 401)
        self.assertEqual(self.api.get('/v1/capabilities', headers={**headers,
            'X-Caller-Id': 'unrecognized'}).status_code, 403)
        self.assertEqual(self.requests, [])

    def test_interaction_payload_recovery_result_and_exact_token_billing(self):
        result = self.submit(payload={'prompt': 'test', 'duration_seconds': 5,
                                     'mode': 'image_to_video', 'image': IMAGE})
        self.assertEqual(result.status_code, 200, result.text)
        first = result.json()
        self.assertTrue(first['accepted'])
        self.assertEqual(first['state'], 'in_progress')
        body = json.loads(self.requests[0].content)
        self.assertEqual(self.requests[0].url.path, '/v1beta/interactions')
        self.assertEqual(body['response_format'], {'type': 'video', 'duration': '5s', 'resolution': '720p',
                                                  'aspect_ratio': '16:9', 'delivery': 'uri'})
        self.assertEqual(body['generation_config'], {'video_config': {'task': 'image_to_video'}})
        self.assertTrue(body['background'])
        self.assertTrue(body['store'])
        self.assertFalse(body['stream'])
        self.assertEqual(body['input'][0], {'type': 'image', **IMAGE})
        self.assertNotIn('key', self.requests[0].url.params)
        complete = self.poll(first)
        self.assertEqual(complete['state'], 'completed')
        self.assertEqual(complete['settlement'], {'amount': '0.40586', 'unit': 'USD', 'source': 'price_table'})
        self.assertEqual(complete['usage'], USAGE)
        self.assertIsNone(complete['pricing']['amount'])
        self.assertEqual(self.api.get('/v1/calls/' + first['call_id'] + '/result', headers=self.headers).content, VIDEO)
        repeated = self.submit(payload={'prompt': 'test', 'duration_seconds': 5, 'mode': 'image_to_video', 'image': IMAGE})
        self.assertEqual(repeated.json(), complete)
        self.assertEqual(sum(r.method == 'POST' for r in self.requests), 1)

    def test_uri_waits_for_active_then_downloads_original_file(self):
        self.mode = 'uri'
        call = self.submit().json()
        self.assertEqual(self.poll(call)['state'], 'in_progress')
        self.assertFalse(any(r.url.path.endswith(':download') for r in self.requests))
        self.ready = True
        done = self.poll(call)
        self.assertEqual(done['state'], 'completed')
        self.assertEqual(sum(r.method == 'POST' for r in self.requests), 1)

    def test_unknown_acceptance_keeps_reservation_and_never_resubmits(self):
        self.mode = 'timeout'
        call = self.submit(budget_limit='6').json()
        self.assertIsNone(call['accepted'])
        self.assertEqual(call['state'], 'acceptance_unknown')
        self.assertEqual(call['error']['code'], 'google_transport_error')
        self.assertEqual(self.submit(budget_limit='6').json(), call)
        self.assertEqual(self.submit('next', budget_limit='6').json()['error'], 'budget_exceeded')
        self.assertEqual(len(self.requests), 1)

    def test_read_failure_preserves_original_identity_and_clears_when_recovered(self):
        call = self.submit().json()
        self.mode = 'timeout'
        interrupted = self.poll(call)
        self.assertEqual(interrupted['state'], 'in_progress')
        self.assertEqual(interrupted['error']['code'], 'google_transport_error')
        self.assertTrue(interrupted['accepted'])
        self.mode = 'inline'
        recovered = self.poll(call)
        self.assertEqual(recovered['state'], 'completed')
        self.assertIsNone(recovered['error'])
        self.assertIsNone(recovered['reason_code'])
        self.assertEqual(sum(r.method == 'POST' for r in self.requests), 1)

    def test_explicit_errors_preserve_safe_provider_reason_and_release_rejected_reserve(self):
        self.mode = 'reject'
        call = self.submit(budget_limit='6').json()
        self.assertFalse(call['accepted'])
        self.assertEqual(call['error']['code'], 'google_invalid_argument')
        self.assertIn('duration is invalid', call['error']['message'])
        self.assertNotIn('synthetic-secret', call['error']['message'])
        self.assertNotIn('unsafe.example', call['error']['message'])
        self.mode = 'empty_reject'
        empty = self.submit('next', budget_limit='6').json()
        self.assertFalse(empty['accepted'])
        self.assertEqual(empty['error']['code'], 'google_http_429')
        self.mode = 'failed'
        failed = self.poll(self.submit('third', budget_limit='6').json())
        self.assertEqual(failed['state'], 'failed')
        self.assertTrue(failed['accepted'])
        self.assertEqual(failed['error']['message'], 'Video quota exceeded.')
        self.assertIsNone(failed['settlement']['amount'])

    def test_preflight_is_read_only_and_checks_real_admission(self):
        request = {'model_id': OMNI_MODEL, 'payload': {'prompt': 'test', 'duration_seconds': 4}, 'budget_limit': '2'}
        for _ in range(2):
            result = self.api.post('/v1/video-preflight', headers=self.headers, json=request).json()
            self.assertTrue(result['can_submit'])
            self.assertEqual(result['quote']['known_amount'], '0.40544')
        self.assertEqual(self.google.calls.list('org')['total'], 0)
        self.assertEqual(self.requests, [])
        missing = self.api.post('/v1/video-preflight', headers=self.headers, json={**request, 'budget_limit': None}).json()
        self.assertEqual(missing['blocked_reason'], 'cost_unknown')
        self.google.accepting = False
        draining = self.api.post('/v1/video-preflight', headers=self.headers, json=request).json()
        self.assertFalse(draining['can_submit'])
        self.assertEqual(draining['blocked_reason'], 'channel_unavailable')

    def test_catalog_controls_new_admission_but_original_call_can_recover(self):
        first = self.submit().json()
        with TestClient(create_execution(self.google, 'e' * 40, video_catalog=[])) as empty:
            self.assertEqual(empty.get('/v1/capabilities', headers=self.headers).json()['models'], [])
            new = empty.post('/v1/calls', headers=self.headers, json={'idempotency_key': 'new', 'task_id': 'new',
                'model_id': OMNI_MODEL, 'capability': 'video', 'payload': {'prompt': 'test', 'duration_seconds': 4}, 'budget_limit': '2'})
            self.assertEqual(new.json()['error'], 'capability_not_supported')
            self.assertEqual(empty.get('/v1/calls/' + first['call_id'], headers=self.headers).json()['state'], 'completed')
        self.assertEqual(sum(r.method == 'POST' for r in self.requests), 1)

    def test_actual_chart_has_one_omni_and_three_retired_video_entries(self):
        chart = Path(__file__).resolve().parents[2] / 'verdantflare-market/charts/google/channel.yaml'
        if not chart.is_file():
            self.skipTest('Market sibling checkout unavailable')
        entries = video_capabilities(chart)
        self.assertEqual([x['model_id'] for x in entries if x['execution_state'] == 'enabled'], [OMNI_MODEL])
        retired = [x for x in entries if x['model_id'] != OMNI_MODEL]
        self.assertEqual(len(retired), 3)
        self.assertTrue(all(x['execution_state'] == 'disabled' and x['reason_code'] == 'model_retired' for x in retired))
