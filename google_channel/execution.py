"""Native synchronous image calls with durable intent and no automatic resubmission."""
import base64
import json

import httpx

from .profile import origin
from .store import Conflict, now

MODEL = 'gemini-3.1-flash-image'
MIMES = {'image/png', 'image/jpeg', 'image/webp'}


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


class Execution:
    def __init__(self, calls, client):
        self.calls, self.client = calls, client
        with calls.store.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS channel_results (
                call_id TEXT PRIMARY KEY, ciphertext BLOB NOT NULL, created_at TEXT NOT NULL)''')

    def submit(self, organization, caller, operation, task, model, value, account, ceiling):
        if model != MODEL:
            raise ValueError('capability_not_supported')
        value = payload(value)
        row, fresh = self.calls.reserve(organization, caller, operation, task, model, value, account, ceiling)
        if not fresh:
            return self.calls.project(row)
        # The persisted route and key version are used, not the account's latest configuration.
        if json.loads(row['proxy_json']) != {'mode': 'direct'}:
            self.calls.rejected(row['id'])
            raise Conflict('proxy_unavailable')
        try:
            base = origin(row['api_url'])
        except ValueError:
            self.calls.rejected(row['id'])
            raise
        key = self.calls.credential(row)
        parts = [{'inlineData': {'mimeType': item['mime_type'], 'data': item['data']}} for item in value['images']]
        parts.append({'text': value['prompt']})
        try:
            with self.client.stream('POST', base + '/v1beta/models/' + MODEL + ':generateContent',
                headers={'x-goog-api-key': key}, json={'contents': [{'parts': parts}],
                'generationConfig': {'responseModalities': ['IMAGE', 'TEXT']}}, timeout=180,
                follow_redirects=False) as result:
                status = result.status_code
                content = bytearray()
                for chunk in result.iter_bytes():
                    content.extend(chunk)
                    if len(content) > 48 * 1024 * 1024:
                        return self.calls.project(row)
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
