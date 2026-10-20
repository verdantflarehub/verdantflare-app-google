"""Local unit checks: mocked provider/proxy, disposable SQLite, no live services."""
import base64
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet
import httpx
from starlette.testclient import TestClient

from google_channel.analysis import payload
from google_channel.metadata import analysis_capabilities
from google_channel.profile import DEFAULT_ORIGIN
from google_channel.server import Google, create_execution
from google_channel.store import Store
from google_channel.proxy import ProxyUnavailable

MODEL = 'gemini-3.1-pro-preview'
CATALOG = [{'model_id': MODEL, 'execution_state': 'enabled'}]
VIDEO = b'\x00\x00\x00\x18ftypisom' + b'synthetic fixture, not a real video'


class AnalysisTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'state.sqlite3'
        self.key = Fernet.generate_key().decode()
        self.mode, self.requests, self.deletes, self.uploaded = 'success', [], 0, b''
        self.client = httpx.Client(transport=httpx.MockTransport(self.upstream))
        self.google = Google(Store(str(self.path), self.key), self.client)
        account = self.google.accounts.create('org', 'user', 'create', {'api_url': DEFAULT_ORIGIN,
            'api_key': 'synthetic-provider-key', 'proxy': {'mode': 'direct'}, 'default': True,
            'registration': {'budget_limit': '10'}})
        self.google.accounts.checked('org', 'user', account['account_id'], 1, 'active')
        self.api = TestClient(create_execution(self.google, 'e' * 40, analysis_token='a' * 40, analysis_enabled=True, analysis_catalog=CATALOG))
        self.headers = {'Authorization': 'Bearer ' + 'a' * 40, 'X-Organization-Id': 'org', 'X-Caller-Id': 'world-analysis'}

    def tearDown(self):
        self.api.close(); self.client.close(); self.temp.cleanup()

    def upstream(self, request):
        self.requests.append(request)
        self.assertEqual(request.url.host, 'generativelanguage.googleapis.com')
        self.assertEqual(request.headers['x-goog-api-key'], 'synthetic-provider-key')
        path = request.url.path
        if request.method == 'DELETE':
            self.deletes += 1
            return httpx.Response(503 if self.mode == 'cleanup_unavailable' else 204)
        if path == '/upload/v1beta/files' and not request.url.query:
            url = DEFAULT_ORIGIN + '/upload/v1beta/files?upload_id=opaque'
            if self.mode == 'foreign_upload':
                url = 'https://invalid.example/upload/v1beta/files'
            return httpx.Response(200, headers={'x-goog-upload-url': url})
        if path == '/upload/v1beta/files':
            self.assertEqual(int(request.headers['x-goog-upload-offset']), len(self.uploaded))
            self.uploaded += request.content
            if request.headers['x-goog-upload-command'] == 'upload':
                return httpx.Response(200)
            self.assertEqual(self.uploaded, VIDEO)
            return httpx.Response(200, json={'file': {'name': 'files/source', 'mimeType': 'video/mp4',
                'uri': DEFAULT_ORIGIN + '/v1beta/files/source', 'state': 'ACTIVE', 'sizeBytes': str(len(VIDEO)),
                'sha256Hash': base64.b64encode(hashlib.sha256(VIDEO if self.mode != 'file_mismatch' else b'wrong').digest()).decode()}})
        self.assertEqual(path, '/v1beta/models/' + MODEL + ':generateContent')
        if self.mode == 'timeout':
            raise httpx.ReadTimeout('synthetic timeout', request=request)
        if self.mode == 'reject':
            return httpx.Response(429)
        if self.mode == 'malformed':
            return httpx.Response(200, json={'candidates': 'invalid'})
        if self.mode == 'blocked':
            return httpx.Response(200, json={'promptFeedback': {'blockReason': 'SAFETY'}})
        text = {'invalid_json': 'not json', 'nan': '{"n":NaN}', 'duplicate': '{"n":1,"n":2}'}.get(self.mode,
            '{"observations":["source observation"],"coverage":"needs verification"}')
        return httpx.Response(200, json={'responseId': 'provider-response', 'modelVersion': 'pinned-model-version',
            'usageMetadata': {'promptTokenCount': 42}, 'candidates': [{'finishReason': 'MAX_TOKENS' if self.mode == 'truncated' else 'STOP',
            'content': {'parts': [{'text': 'do not expose internal reasoning', 'thought': True}, {'text': text}]}}]})

    def submit(self, operation='analysis-task', media=False, **updates):
        value = {'prompt': 'Describe the supplied source as JSON.', 'source_text': 'A character enters the scene.'}
        if media:
            value = {'prompt': 'Describe the whole video and audio as JSON.', 'video_fps': 2,
                     'media': {'mime_type': 'video/mp4', 'data': base64.b64encode(VIDEO).decode(),
                               'sha256': hashlib.sha256(VIDEO).hexdigest()}}
        request = {'idempotency_key': operation, 'task_id': operation, 'model_id': MODEL,
                   'capability': 'analysis', 'payload': value, 'budget_limit': '6', **updates}
        return self.api.post('/v1/calls', headers=self.headers, json=request)

    def result(self, row):
        return self.api.get('/v1/calls/' + row['call_id'] + '/result', headers=self.headers)

    def test_complete_media_transport_encrypted_result_and_same_operation_replay(self):
        response = self.submit(media=True)
        self.assertEqual(response.status_code, 200, response.text)
        row = response.json()
        self.assertEqual(row['state'], 'completed')
        self.assertEqual(row['operation'], 'analyze')
        output = self.result(row).json()
        self.assertFalse(output['analysis']['domain_coverage_verified'])
        self.assertEqual(output['analysis']['input']['media']['sha256'], hashlib.sha256(VIDEO).hexdigest())
        self.assertEqual(output['source_cleanup'], 'deleted')
        self.assertNotIn(b'source observation', self.path.read_bytes())
        count = len(self.requests)
        self.assertEqual(self.submit(media=True).json(), row)
        self.assertEqual(len(self.requests), count)
        request = next(r for r in self.requests if ':generateContent' in r.url.path)
        body = json.loads(request.content)
        self.assertEqual(body['contents'][0]['parts'][0]['videoMetadata'], {'fps': 2})
        self.assertEqual(body['contents'][0]['parts'][0]['fileData']['mimeType'], 'video/mp4')
        self.assertIn('untrusted reference', body['systemInstruction']['parts'][0]['text'])
        reopened = Google(Store(str(self.path), self.key), self.client)
        saved = reopened.execution.analysis.result('org', 'world-analysis', row['call_id'])
        self.assertEqual(saved['analysis'], output['analysis'])
        self.assertNotIn('do not expose internal reasoning', saved['analysis']['text'])

    def test_timeout_preserves_reservation_and_never_repeats_inference(self):
        self.mode = 'timeout'
        row = self.submit().json()
        self.assertEqual(row['state'], 'acceptance_unknown')
        self.assertIsNone(row['accepted'])
        self.assertEqual(self.submit().json(), row)
        self.assertEqual(self.submit('new-task').json()['error'], 'budget_exceeded')
        self.assertEqual(len(self.requests), 1)
        reopened = Google(Store(str(self.path), self.key), self.client)
        self.assertEqual(reopened.execution.refresh('org', 'world-analysis', row['call_id']), row)
        self.assertEqual(len(self.requests), 1)

    def test_resumable_chunks_keep_offset_bytes_and_finalize_once(self):
        with patch('google_channel.analysis.UPLOAD_CHUNK_BYTES', 16):
            row = self.submit(media=True).json()
        self.assertEqual(row['state'], 'completed')
        self.assertEqual(self.uploaded, VIDEO)
        chunks = [r for r in self.requests if r.headers.get('x-goog-upload-offset') is not None]
        self.assertGreater(len(chunks), 1)
        self.assertEqual(sum(r.headers['x-goog-upload-command'] == 'upload, finalize' for r in chunks), 1)

    def test_provider_file_hash_mismatch_cannot_reach_inference(self):
        self.mode = 'file_mismatch'
        row = self.submit(media=True).json()
        self.assertEqual(row['state'], 'rejected')
        self.assertEqual(row['reason_code'], 'provider_file_mismatch')
        self.assertFalse(any(':generateContent' in str(r.url) for r in self.requests))

    def test_incomplete_or_invalid_output_never_completes(self):
        for mode in ('truncated', 'invalid_json', 'nan', 'duplicate', 'blocked'):
            with self.subTest(mode=mode):
                self.mode = mode
                row = self.submit(mode, budget_limit='1').json()
                self.assertEqual(row['state'], 'failed', row)
                self.assertTrue(row['accepted'])
                self.assertEqual(self.result(row).status_code, 200)
                count = len(self.requests)
                self.assertEqual(self.submit(mode, budget_limit='1').json(), row)
                self.assertEqual(len(self.requests), count)

    def test_malformed_success_is_unknown(self):
        self.mode = 'malformed'
        row = self.submit().json()
        self.assertEqual(row['state'], 'acceptance_unknown')
        self.assertEqual(self.submit().json(), row)
        self.assertEqual(len(self.requests), 1)

    def test_foreign_upload_url_rejected_before_inference_or_key_forward(self):
        self.mode = 'foreign_upload'
        row = self.submit(media=True).json()
        self.assertEqual(row['state'], 'rejected')
        self.assertEqual(row['reason_code'], 'invalid_provider_url')
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.deletes, 0)

    def test_proxy_unavailable_has_no_direct_fallback(self):
        def unavailable(*_):
            raise ProxyUnavailable()
        self.google.execution.analysis.transport = unavailable
        row = self.submit().json()
        self.assertEqual(row['state'], 'rejected')
        self.assertFalse(self.requests)

    def test_cleanup_retries_only_delete_and_does_not_regenerate(self):
        self.mode = 'cleanup_unavailable'
        row = self.submit(media=True).json()
        self.assertEqual(row['state'], 'completed')
        self.assertEqual(self.result(row).json()['source_cleanup'], 'pending')
        self.mode = 'success'
        count = len(self.requests)
        self.api.get('/v1/calls/' + row['call_id'], headers=self.headers)
        self.assertEqual(len(self.requests), count + 1)
        self.assertEqual(self.requests[-1].method, 'DELETE')
        self.assertEqual(self.result(row).json()['source_cleanup'], 'deleted')

    def test_separate_token_organization_and_operation_identity(self):
        row = self.submit().json()
        path = '/v1/calls/' + row['call_id'] + '/result'
        self.assertEqual(self.api.get(path, headers={**self.headers, 'Authorization': 'Bearer ' + 'e' * 40}).status_code, 401)
        self.assertEqual(self.api.get(path, headers={**self.headers, 'X-Organization-Id': 'other'}).status_code, 404)
        self.assertEqual(self.api.get(path, headers={**self.headers, 'Authorization': 'Bearer ' + 'e' * 40, 'X-Caller-Id': 'image-mcp'}).status_code, 403)
        self.assertEqual(self.submit(payload={'prompt': 'Different', 'source_text': 'Changed'}).status_code, 409)
        with self.assertRaises(ValueError):
            create_execution(self.google, 'e' * 40, analysis_token='e' * 40, analysis_enabled=True, analysis_catalog=CATALOG)

    def test_admission_disabled_preserves_prior_results(self):
        row = self.submit().json()
        with TestClient(create_execution(self.google, 'e' * 40, analysis_token='a' * 40)) as api:
            self.assertEqual(api.get('/v1/analysis/capabilities', headers=self.headers).json()['execution_state'], 'disabled')
            self.assertEqual(api.post('/v1/calls', headers=self.headers, json={}).status_code, 503)
            self.assertEqual(api.get('/v1/calls/' + row['call_id'] + '/result', headers=self.headers).status_code, 200)

    def test_unverified_or_missing_chart_declaration_cannot_submit(self):
        for catalog in ([], [dict(CATALOG[0], execution_state='adapter_connected_unverified')]):
            with TestClient(create_execution(self.google, 'e' * 40, analysis_token='a' * 40,
                                            analysis_enabled=True, analysis_catalog=catalog)) as api:
                snapshot = api.get('/v1/analysis/capabilities', headers=self.headers).json()
                self.assertEqual(snapshot['models'], [])
                self.assertEqual(snapshot['execution_state'], 'disabled')
                reply = api.post('/v1/calls', headers=self.headers, json={'capability': 'analysis', 'model_id': MODEL})
                self.assertEqual(reply.status_code, 400)
        self.assertFalse(self.requests)

    def test_chart_metadata_does_not_enable_unsupported_or_duplicate_models(self):
        path = Path(self.temp.name) / 'channel.yaml'
        item = dict(CATALOG[0], type='analysis', operation='analysis.analyze',
                    capability_id='analysis.analyze', parameter_schema={'type': 'object'})
        doc = {'schema_version': 1, 'channel_id': 'google', 'capabilities': [item]}
        path.write_text(json.dumps(doc))
        self.assertEqual(analysis_capabilities(path), [item])
        for entries in ([item, item], [dict(item, model_id='unknown')], [dict(item, operation='image.generate')]):
            path.write_text(json.dumps(dict(doc, capabilities=entries)))
            with self.assertRaises(ValueError):
                analysis_capabilities(path)
        self.assertEqual(analysis_capabilities(None), [])

    def test_input_digest_mime_and_sampling_validated_before_reservation(self):
        for value in ({'prompt': 'x'}, {'prompt': 'x', 'source_text': 'text', 'video_fps': 2},
                      {'prompt': 'x', 'source_text': 'text', 'max_output_tokens': True},
                      {'prompt': 'x', 'media': {'mime_type': 'video/mp4', 'data': base64.b64encode(VIDEO).decode(), 'sha256': '0' * 64}},
                      {'prompt': 'x', 'media': {'mime_type': 'image/png', 'data': base64.b64encode(VIDEO).decode(), 'sha256': hashlib.sha256(VIDEO).hexdigest()}}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                payload(value)
        self.assertFalse(self.requests)


if __name__ == '__main__':
    unittest.main()
