"""Explicit Omni interactions with durable identity and bounded, recoverable results."""
import base64
from contextlib import nullcontext
from decimal import Decimal
import json
import re
from urllib.parse import quote, urlsplit

import httpx

from .calls import amount
from .profile import origin
from .proxy import ProxyUnavailable
from .store import Conflict
from .video_pricing import OMNI_MODEL, estimate, settlement

MAX_RESPONSE_BYTES = 360 * 1024 * 1024
MAX_VIDEO_BYTES = 256 * 1024 * 1024
INTERACTION_ID = re.compile(r'[A-Za-z0-9_-]{1,512}\Z')


def payload(value, image_validator):
    if not isinstance(value, dict) or set(value) - {
            'prompt', 'duration', 'duration_seconds', 'aspect_ratio', 'resolution', 'mode', 'image'}:
        raise Conflict('invalid_video_payload')
    prompt = value.get('prompt')
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 16000:
        raise Conflict('invalid_prompt')
    duration = value.get('duration')
    seconds = value.get('duration_seconds')
    if duration is not None and seconds is not None:
        raise Conflict('ambiguous_duration')
    if seconds is not None:
        if type(seconds) is not int:
            raise Conflict('unsupported_duration')
        duration = f'{seconds}s'
    if duration is None:
        raise Conflict('duration_required')
    if not isinstance(duration, str) or not re.fullmatch(r'(?:[3-9]|10)s', duration):
        raise Conflict('unsupported_duration')
    resolution = value.get('resolution', '720p')
    if resolution != '720p':
        raise Conflict('unsupported_resolution')
    aspect = value.get('aspect_ratio', '16:9')
    if aspect not in {'16:9', '9:16'}:
        raise Conflict('unsupported_aspect_ratio')
    mode = value.get('mode', 'text_to_video')
    if mode not in {'text_to_video', 'image_to_video'}:
        raise Conflict('unsupported_mode')
    image = value.get('image')
    if (mode == 'image_to_video') != (image is not None):
        raise Conflict('invalid_reference_count')
    result = {'prompt': prompt.strip(), 'duration': duration, 'resolution': resolution,
              'aspect_ratio': aspect, 'mode': mode}
    if image is not None:
        result['image'] = image_validator(image)
    return result


def provider_error(value, status=None):
    error = value.get('error') if isinstance(value, dict) else None
    if not isinstance(error, dict):
        error = {}
    raw_status = error.get('status')
    safe_status = raw_status if isinstance(raw_status, str) and re.fullmatch(r'[A-Z_]{1,80}', raw_status) else None
    code = 'google_' + safe_status.lower() if safe_status else ('google_http_' + str(status) if status else 'google_generation_failed')
    message = error.get('message')
    if not isinstance(message, str):
        message = 'Google did not return a usable video result.'
    message = re.sub(r'https?://\S+', '[redacted_url]', message)
    message = re.sub(r'(?i)(?:bearer\s+\S+|AIza[A-Za-z0-9_-]+|(?:api[_ -]?key|token|authorization)\s*[:=]\s*\S+)',
                     '[redacted_credential]', message)
    message = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', message)[:1200]
    return {'code': code, 'message': message, 'provider_status': safe_status,
            'http_status': status, 'source': 'google', 'retryable': status == 429 or bool(status and status >= 500)}


def recovery_error(error):
    code = 'google_transport_error' if isinstance(error, httpx.HTTPError) else 'google_result_unavailable'
    return {'code': code, 'message': 'Google interaction or video result could not be read. Query the original call to recover.',
            'source': 'google', 'retryable': True}


class OmniExecution:
    def __init__(self, parent):
        self.parent = parent

    @property
    def calls(self):
        return self.parent.calls

    def transport(self, organization, row):
        proxy = json.loads(row['proxy_json'])
        if self.parent.transport:
            return self.parent.transport(organization, proxy)
        if proxy != {'mode': 'direct'}:
            raise ProxyUnavailable()
        return nullcontext(self.parent.client)

    def preflight(self, organization, value, account, ceiling):
        pricing = estimate(value)
        reason = None
        try:
            if ceiling is None or Decimal(amount(ceiling)) <= 0:
                raise Conflict('cost_unknown')
            if Decimal(ceiling) < Decimal(pricing['known_amount']):
                raise Conflict('budget_exceeded')
            self.calls.preview(organization, account, ceiling)
        except Conflict as exc:
            reason = str(exc)
        return {'schema_version': 1, 'channel_id': 'google', 'model_id': OMNI_MODEL,
                'can_submit': reason is None, 'blocked_reason': reason, 'quote': pricing,
                'parameters': {key: value[key] for key in ('mode', 'duration', 'resolution', 'aspect_ratio')}}

    def submit(self, organization, caller, operation, task, value, account, ceiling):
        # Idempotency remains valid when a price table expires after the original request.
        try:
            previous = self.calls.by_operation(organization, caller, operation)
        except KeyError:
            previous = None
        if previous is not None:
            row, _ = self.calls.reserve(organization, caller, operation, task, OMNI_MODEL, value,
                                        account, ceiling, media='video')
            return self.calls.project(row)
        pricing = estimate(value)
        if ceiling is None or Decimal(amount(ceiling)) < Decimal(pricing['known_amount']):
            raise Conflict('budget_exceeded' if ceiling is not None else 'cost_unknown')
        row, fresh = self.calls.reserve(organization, caller, operation, task, OMNI_MODEL, value,
                                        account, ceiling, media='video', pricing=pricing)
        if not fresh:
            return self.calls.project(row)
        data = [{'type': 'text', 'text': value['prompt']}]
        if value.get('image'):
            data.insert(0, {'type': 'image', 'mime_type': value['image']['mime_type'], 'data': value['image']['data']})
        body = {'model': OMNI_MODEL, 'input': data,
                'response_format': {'type': 'video', 'duration': value['duration'], 'resolution': value['resolution'],
                                    'aspect_ratio': value['aspect_ratio'], 'delivery': 'uri'},
                'generation_config': {'video_config': {'task': value['mode']}},
                'background': True, 'store': True, 'stream': False}
        try:
            base = origin(row['api_url'])
            key = self.calls.credential(row)
            with self.transport(organization, row) as client:
                with client.stream('POST', base + '/v1beta/interactions', headers={'x-goog-api-key': key},
                                   json=body, timeout=120, follow_redirects=False) as result:
                    status = result.status_code
                    try:
                        response = self.read_json(result)
                    except (ValueError, TypeError):
                        if status not in {400, 401, 403, 404, 422, 429}:
                            raise
                        response = {}
                if status in {400, 401, 403, 404, 422, 429}:
                    error = provider_error(response, status)
                    self.calls.rejected(row['id'], error['code'])
                    self.calls.video_details(row['id'], error=error)
                elif 200 <= status < 300:
                    interaction_id = response.get('id')
                    if isinstance(interaction_id, str) and INTERACTION_ID.fullmatch(interaction_id):
                        self.calls.accepted(row['id'], interaction_id)
                        row['provider_request_id'] = interaction_id
                        self.consume(client, base, key, row, response)
                else:
                    self.calls.video_details(row['id'], error=provider_error(response, status))
        except ProxyUnavailable:
            self.calls.rejected(row['id'], 'proxy_unavailable')
            raise Conflict('proxy_unavailable') from None
        except (httpx.HTTPError, ValueError, TypeError, KeyError, OSError) as exc:
            # A request may already be accepted; never send a second create.
            self.calls.video_details(row['id'], error=recovery_error(exc))
        return self.calls.project(self.calls.get(organization, row['id'], caller))

    @staticmethod
    def read_json(response):
        raw = bytearray()
        for chunk in response.iter_bytes():
            raw.extend(chunk)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ValueError('response_too_large')
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError('invalid_provider_response')
        return parsed

    def refresh(self, organization, caller, call_id):
        row = self.calls.get(organization, call_id, caller)
        if row['state'] in {'completed', 'failed', 'rejected'} or not row.get('provider_request_id'):
            return self.calls.project(row)
        remote = row['provider_request_id']
        if not INTERACTION_ID.fullmatch(remote):
            raise Conflict('invalid_provider_request_id')
        try:
            base, key = origin(row['api_url']), self.calls.credential(row)
            with self.transport(organization, row) as client:
                with client.stream('GET', base + '/v1beta/interactions/' + quote(remote, safe=''),
                                   headers={'x-goog-api-key': key}, timeout=120, follow_redirects=False) as response:
                    try:
                        data = self.read_json(response)
                    except (ValueError, TypeError):
                        if 200 <= response.status_code < 300:
                            raise
                        data = {}
                    if not 200 <= response.status_code < 300:
                        self.calls.video_details(call_id, error=provider_error(data, response.status_code))
                        return self.calls.project(self.calls.get(organization, call_id, caller))
                self.consume(client, base, key, row, data)
        except ProxyUnavailable:
            raise Conflict('proxy_unavailable') from None
        except (httpx.HTTPError, ValueError, TypeError, KeyError, OSError) as exc:
            self.calls.video_details(call_id, error=recovery_error(exc))
        return self.calls.project(self.calls.get(organization, call_id, caller))

    def consume(self, client, base, key, row, data):
        if data.get('id') != row['provider_request_id'] or data.get('model', OMNI_MODEL) != OMNI_MODEL:
            raise ValueError('provider_identity_mismatch')
        pricing = json.loads(row.get('pricing_json') or '{}')
        bill, usage = settlement(data.get('usage') or json.loads(row.get('usage_json') or '{}'), pricing.get('price_version'))
        self.calls.video_details(row['id'], usage=usage, settlement_details=bill)
        status = data.get('status')
        if data.get('error') or status in {'failed', 'cancelled', 'incomplete', 'requires_action'}:
            error = provider_error(data)
            self.calls.video_details(row['id'], error=error)
            self.calls.observed(row['id'], 'failed')
            return
        if status != 'completed':
            self.calls.observed(row['id'], 'in_progress')
            return
        videos = []
        for step in data.get('steps', []):
            if not isinstance(step, dict) or step.get('type') != 'model_output':
                continue
            content = step.get('content', [])
            if not isinstance(content, list):
                raise ValueError('invalid_provider_output')
            videos.extend(item for item in content if isinstance(item, dict) and item.get('type') == 'video')
        if len(videos) != 1:
            self.calls.video_details(row['id'], error={'code': 'no_video_returned',
                'message': 'Google completed the interaction without exactly one video.', 'source': 'google'})
            self.calls.observed(row['id'], 'failed')
            return
        video = videos[0]
        if video.get('mime_type') not in {None, 'video/mp4'}:
            raise ValueError('invalid_video_type')
        if isinstance(video.get('data'), str):
            raw = base64.b64decode(video['data'], validate=True)
        elif isinstance(video.get('uri'), str):
            uri = video['uri']
            parsed = urlsplit(uri)
            if (parsed.scheme != 'https' or parsed.netloc != urlsplit(base).netloc
                    or not re.fullmatch(r'/v1beta/files/[A-Za-z0-9_-]+(?::download)?', parsed.path)
                    or parsed.username or parsed.password):
                raise ValueError('invalid_video_url')
            file_id = parsed.path.rsplit('/', 1)[-1].split(':', 1)[0]
            with client.stream('GET', base + '/v1beta/files/' + file_id, headers={'x-goog-api-key': key},
                               timeout=60, follow_redirects=False) as file_response:
                if file_response.status_code != 200:
                    raise ValueError('file_status_unavailable')
                file_status = self.read_json(file_response).get('state')
            if file_status == 'FAILED':
                self.calls.video_details(row['id'], error={'code': 'google_file_failed',
                    'message': 'Google could not prepare the generated video file.', 'source': 'google'})
                self.calls.observed(row['id'], 'failed')
                return
            if file_status != 'ACTIVE':
                self.calls.observed(row['id'], 'in_progress')
                return
            raw = self.parent._download_video(client, base, key, base + '/v1beta/files/' + file_id + ':download?alt=media')
        else:
            raise ValueError('video_content_missing')
        if not raw or len(raw) > MAX_VIDEO_BYTES or b'ftyp' not in raw[:32]:
            raise ValueError('invalid_video_payload')
        target = self.parent.video_dir / (row['id'] + '.bin')
        temporary = target.with_suffix('.bin.tmp')
        temporary.write_bytes(self.calls.store.cipher.encrypt(raw))
        temporary.replace(target)
        self.calls.complete_video(row['id'], bill['amount'], settlement_source=bill['source'])
