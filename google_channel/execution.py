"""Native synchronous image and asynchronous Veo 3.1 video calls with durable intent."""
import base64
import json
from contextlib import nullcontext
from pathlib import Path
from urllib.parse import urlsplit
from .proxy import ProxyUnavailable

import httpx

from .profile import origin
from .store import Conflict, now

MODEL = 'gemini-3.1-flash-image'
IMAGE_MODELS = {
    'gemini-3.1-flash-image',
    'gemini-3.1-flash-image-preview',
    'gemini-3-pro-image-preview',
    'gemini-2.5-flash-image',
}
VIDEO_MODELS = {
    'veo-3.1-lite-generate-preview',
    'veo-3.1-fast-generate-preview',
    'veo-3.1-generate-preview',
}
VIDEO_ASPECTS = {'16:9', '9:16'}
MIMES = {'image/png', 'image/jpeg', 'image/webp'}
MAX_VIDEO_BYTES = 256 * 1024 * 1024


def image(value):
    if not isinstance(value, dict) or set(value) != {'mime_type', 'data'} or value['mime_type'] not in MIMES:
        raise ValueError('invalid_image')
    if not isinstance(value['data'], str) or not 1 <= len(value['data']) <= 32 * 1024 * 1024:
        raise ValueError('invalid_image')
    data = base64.b64decode(value['data'], validate=True)
    matches = {'image/png': data.startswith(b'\x89PNG\r\n\x1a\n'),
               'image/jpeg': data.startswith(b'\xff\xd8\xff'),
               'image/webp': data.startswith(b'RIFF') and data[8:12] == b'WEBP'}
    if not matches[value['mime_type']]:
        raise ValueError('invalid_image')
    return value


def payload(value):
    if not isinstance(value, dict) or set(value) - {'prompt', 'images'}:
        raise ValueError('invalid_payload')
    prompt = value.get('prompt')
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 32000:
        raise ValueError('invalid_prompt')
    images = value.get('images', [])
    if not isinstance(images, list) or len(images) > 12:
        raise ValueError('invalid_references')
    return {'prompt': prompt, 'images': [image(item) for item in images]}


def video_payload(value):
    if not isinstance(value, dict) or set(value) - {
        'prompt', 'duration_seconds', 'duration', 'aspect_ratio', 'resolution', 'mode', 'image'
    }:
        raise ValueError('invalid_payload')
    prompt = value.get('prompt')
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 16000:
        raise ValueError('invalid_prompt')
    duration = value.get('duration_seconds', value.get('duration', 6))
    if type(duration) is not int or not 1 <= duration <= 15:
        raise ValueError('unsupported_parameter')
    veo_duration = 4 if duration <= 4 else (6 if duration <= 6 else 8)
    aspect_ratio = value.get('aspect_ratio', '16:9')
    if aspect_ratio not in VIDEO_ASPECTS:
        raise ValueError('unsupported_parameter')
    resolution = value.get('resolution', '720p')
    if resolution not in {'480p', '720p', '1080p', '4k'}:
        raise ValueError('unsupported_parameter')
    veo_resolution = '720p' if resolution == '480p' else resolution
    mode = value.get('mode', 'text_to_video')
    if mode not in {'text_to_video', 'image_to_video'}:
        raise ValueError('unsupported_parameter')
    result = {
        'prompt': prompt.strip(),
        'duration_seconds': veo_duration,
        'aspect_ratio': aspect_ratio,
        'resolution': veo_resolution,
        'mode': mode,
    }
    if value.get('image') is not None:
        result['image'] = image(value['image'])
    return result


class Execution:
    def __init__(self, calls, client, *, transport=None):
        self.calls, self.client = calls, client
        self.transport = transport
        self.video_dir = Path(calls.store.path).parent / 'video_results'
        self.video_dir.mkdir(parents=True, exist_ok=True)
        with calls.store.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS channel_results (
                call_id TEXT PRIMARY KEY, ciphertext BLOB NOT NULL, created_at TEXT NOT NULL)''')

    def submit(self, organization, caller, operation, task, model, value, account, ceiling, *, capability='image'):
        if capability == 'video':
            return self.submit_video(organization, caller, operation, task, model, value, account, ceiling)
        if model not in IMAGE_MODELS:
            raise ValueError('capability_not_supported')
        value = payload(value)
        row, fresh = self.calls.reserve(organization, caller, operation, task, model, value, account, ceiling, media='image')
        if not fresh:
            return self.calls.project(row)
        # The persisted route and key version are used, not the account's latest configuration.
        try:
            base = origin(row['api_url'])
        except ValueError:
            self.calls.rejected(row['id'])
            raise
        key = self.calls.credential(row)
        parts = [{'inlineData': {'mimeType': item['mime_type'], 'data': item['data']}} for item in value['images']]
        parts.append({'text': value['prompt']})
        req_body = {'contents': [{'parts': parts}], 'generationConfig': {'responseModalities': ['IMAGE', 'TEXT']}}
        try:
            if self.transport is None and json.loads(row['proxy_json']) != {'mode': 'direct'}:
                raise ProxyUnavailable()
            with self.transport(organization, json.loads(row['proxy_json'])) if self.transport else nullcontext(self.client) as client:
                with client.stream('POST', base + '/v1beta/models/' + model + ':generateContent',
                    headers={'x-goog-api-key': key}, json=req_body, timeout=180,
                    follow_redirects=False) as result:
                    status = result.status_code
                    content = bytearray()
                    for chunk in result.iter_bytes():
                        content.extend(chunk)
                        if len(content) > 48 * 1024 * 1024:
                            return self.calls.project(row)
                if status == 404 and model == 'gemini-3.1-flash-image' and b'not found' in bytes(content).lower():
                    with client.stream('POST', base + '/v1beta/models/gemini-3.1-flash-image-preview:generateContent',
                        headers={'x-goog-api-key': key}, json=req_body, timeout=180,
                        follow_redirects=False) as result:
                        status = result.status_code
                        content = bytearray()
                        for chunk in result.iter_bytes():
                            content.extend(chunk)
                            if len(content) > 48 * 1024 * 1024:
                                return self.calls.project(row)
        except ProxyUnavailable:
            self.calls.rejected(row['id'])
            raise Conflict('proxy_unavailable') from None
        except httpx.HTTPError:
            return self.calls.project(row)
        if status in {400, 401, 403, 404, 422, 429}:
            self.calls.rejected(row['id'])
        elif 200 <= status < 300:
            try:
                data = json.loads(content)
                candidates = data.get('candidates', [])
                output = None
                safety_block = False
                if not isinstance(candidates, list):
                    raise ValueError('invalid_response')
                for candidate in candidates:
                    if candidate.get('finishReason') in {'SAFETY', 'IMAGE_SAFETY', 'IMAGE_OTHER'}:
                        safety_block = True
                        continue
                    for part in candidate.get('content', {}).get('parts', []):
                        inline = part.get('inlineData')
                        if inline:
                            output = image({'mime_type': inline.get('mimeType'), 'data': inline.get('data')})
                            break
                    if output:
                        break
                if output or safety_block:
                    remote = data.get('responseId')
                    remote = remote if isinstance(remote, str) and len(remote) <= 256 else None
                    self.finish(row['id'], output, remote)
            except (ValueError, TypeError, AttributeError):
                # A malformed response cannot prove that no charge or generation occurred.
                pass
        return self.calls.project(self.calls.get(organization, row['id'], caller))

    def submit_video(self, organization, caller, operation, task, model, value, account, ceiling):
        if model not in VIDEO_MODELS:
            raise ValueError('capability_not_supported')
        value = video_payload(value)
        row, fresh = self.calls.reserve(organization, caller, operation, task, model, value, account, ceiling, media='video')
        if not fresh:
            return self.calls.project(row)
        try:
            base = origin(row['api_url'])
        except ValueError:
            self.calls.rejected(row['id'])
            raise
        key = self.calls.credential(row)
        instance = {'prompt': value['prompt']}
        if value.get('image'):
            instance['image'] = {
                'bytesBase64Encoded': value['image']['data'],
                'mimeType': value['image']['mime_type'],
            }
        parameters = {
            'aspectRatio': value['aspect_ratio'],
            'durationSeconds': value['duration_seconds'],
            'resolution': value['resolution'],
        }
        try:
            if self.transport is None and json.loads(row['proxy_json']) != {'mode': 'direct'}:
                raise ProxyUnavailable()
            with self.transport(organization, json.loads(row['proxy_json'])) if self.transport else nullcontext(self.client) as client:
                with client.stream('POST', f'{base}/v1beta/models/{model}:predictLongRunning',
                                   headers={'x-goog-api-key': key},
                                   json={'instances': [instance], 'parameters': parameters},
                                   timeout=120, follow_redirects=False) as result:
                    status = result.status_code
                    content = bytearray()
                    for chunk in result.iter_bytes():
                        content.extend(chunk)
                        if len(content) > 1024 * 1024:
                            return self.calls.project(row)
        except ProxyUnavailable:
            self.calls.rejected(row['id'])
            raise Conflict('proxy_unavailable') from None
        except httpx.HTTPError:
            return self.calls.project(row)
        if status in {400, 401, 403, 404, 422, 429}:
            self.calls.rejected(row['id'])
        elif 200 <= status < 300:
            try:
                data = json.loads(content)
                op_name = data.get('name')
                if isinstance(op_name, str) and op_name.strip():
                    self.calls.accepted(row['id'], op_name.strip())
                    self.calls.observed(row['id'], 'in_progress')
            except (ValueError, TypeError, AttributeError):
                pass
        return self.calls.project(self.calls.get(organization, row['id'], caller))

    @staticmethod
    def _download_video(client, base, key, url):
        parsed_base = urlsplit(base)
        target = url if not url.startswith('/') else f'{parsed_base.scheme}://{parsed_base.netloc}{url}'
        for _ in range(4):
            parsed = urlsplit(target)
            if parsed.scheme not in {'http', 'https'} or not parsed.hostname:
                raise ValueError('invalid_video_url')
            same_origin = (parsed.scheme == parsed_base.scheme and parsed.netloc == parsed_base.netloc)
            headers = {'x-goog-api-key': key} if same_origin else {}
            with client.stream('GET', target, headers=headers, follow_redirects=False, timeout=180) as res:
                if res.status_code in {301, 302, 303, 307, 308}:
                    location = res.headers.get('location', '')
                    if not location:
                        raise ValueError('redirect_missing_location')
                    target = location if not location.startswith('/') else f'{parsed.scheme}://{parsed.netloc}{location}'
                    continue
                if res.status_code != 200:
                    raise ValueError(f'download_http_{res.status_code}')
                raw = bytearray()
                for chunk in res.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > MAX_VIDEO_BYTES:
                        raise ValueError('video_size_exceeded')
                if not raw or b'ftyp' not in bytes(raw[:32]):
                    raise ValueError('invalid_video_payload')
                return bytes(raw)
        raise ValueError('too_many_redirects')

    def refresh(self, organization, caller, call_id):
        row = self.calls.get(organization, call_id, caller)
        if (row.get('media') != 'video' or row['state'] in {'completed', 'failed', 'rejected'}
                or not row.get('provider_request_id')):
            return self.calls.project(row)
        try:
            base = origin(row['api_url'])
            key = self.calls.credential(row)
            if self.transport is None and json.loads(row['proxy_json']) != {'mode': 'direct'}:
                raise ProxyUnavailable()
            with self.transport(organization, json.loads(row['proxy_json'])) if self.transport else nullcontext(self.client) as client:
                op_name = row['provider_request_id'].lstrip('/')
                with client.stream('GET', f'{base}/v1beta/{op_name}',
                                   headers={'x-goog-api-key': key},
                                   follow_redirects=False, timeout=60) as result:
                    if result.status_code == 404:
                        self.calls.observed(call_id, 'failed')
                        return self.calls.project(self.calls.get(organization, call_id, caller))
                    if not (200 <= result.status_code < 300):
                        return self.calls.project(row)
                    raw = bytearray()
                    for chunk in result.iter_bytes():
                        raw.extend(chunk)
                        if len(raw) > 2 * 1024 * 1024:
                            raise ValueError('response_too_large')
                data = json.loads(raw)
                if data.get('error'):
                    self.calls.observed(call_id, 'failed')
                elif data.get('done') is True:
                    resp = data.get('response') if isinstance(data.get('response'), dict) else {}
                    gen_resp = resp.get('generateVideoResponse') if isinstance(resp.get('generateVideoResponse'), dict) else resp
                    samples = gen_resp.get('generatedSamples') or gen_resp.get('videos') or []
                    video_uri = None
                    if isinstance(samples, list) and samples:
                        first = samples[0] if isinstance(samples[0], dict) else {}
                        vobj = first.get('video') if isinstance(first.get('video'), dict) else first
                        video_uri = vobj.get('uri') or vobj.get('url')
                    if not isinstance(video_uri, str) or not video_uri.strip():
                        self.calls.observed(call_id, 'failed')
                    else:
                        video_bytes = self._download_video(client, base, key, video_uri.strip())
                        target_path = self.video_dir / f'{call_id}.bin'
                        temp_path = self.video_dir / f'{call_id}.bin.tmp'
                        temp_path.write_bytes(self.calls.store.cipher.encrypt(video_bytes))
                        temp_path.replace(target_path)
                        self.calls.complete_video(call_id)
                else:
                    self.calls.observed(call_id, 'in_progress')
        except ProxyUnavailable:
            raise Conflict('proxy_unavailable') from None
        except (httpx.HTTPError, ValueError, TypeError, KeyError, OSError):
            pass
        return self.calls.project(self.calls.get(organization, call_id, caller))

    def finish(self, call_id, output, provider_id):
        timestamp = now()
        with self.calls.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT state FROM channel_calls WHERE id=?', (call_id,)).fetchone()
            if row is None or row['state'] != 'acceptance_unknown':
                return
            if output:
                ciphertext = self.calls.store.cipher.encrypt(json.dumps(output).encode())
                db.execute('INSERT INTO channel_results VALUES (?,?,?)', (call_id, ciphertext, timestamp))
            db.execute('UPDATE channel_calls SET state=?,accepted=1,provider_request_id=?,updated_at=?,finished_at=?, '
                       'revision=revision+1 WHERE id=?',
                       ('completed' if output else 'failed', provider_id, timestamp, timestamp, call_id))

    def result(self, organization, caller, call_id):
        row = self.calls.get(organization, call_id, caller)
        with self.calls.store.connect() as db:
            result = db.execute('SELECT ciphertext FROM channel_results WHERE call_id=?', (call_id,)).fetchone()
        if result is None:
            raise Conflict('result_not_available')
        output = json.loads(self.calls.store.cipher.decrypt(result['ciphertext']))
        return {**self.calls.project(row), 'image': output}

    def video_result(self, organization, caller, call_id) -> bytes:
        row = self.calls.get(organization, call_id, caller)
        if row.get('state') != 'completed':
            self.refresh(organization, caller, call_id)
            row = self.calls.get(organization, call_id, caller)
        path = self.video_dir / f'{call_id}.bin'
        if row.get('state') != 'completed' or not path.is_file():
            raise Conflict('result_not_available')
        return self.calls.store.cipher.decrypt(path.read_bytes())
