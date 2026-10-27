"""Read channel capability declarations from the installed Chart; adapter limits stay in code."""
import hashlib
import json
from pathlib import Path

import yaml

from .analysis import MODELS
from .execution import IMAGE_MODELS, VIDEO_MODELS
from .video_pricing import RETIRED_VIDEO_MODELS, RETIREMENT_DATE, RETIREMENT_SOURCE

_GENERATE_SCHEMA = {
    'type': 'object',
    'additionalProperties': False,
    'required': ['prompt'],
    'properties': {
        'prompt': {'type': 'string', 'minLength': 1, 'maxLength': 32000},
        'aspect_ratio': {'type': 'string', 'enum': ['1:1', '2:3', '3:2', '3:4', '4:3', '9:16', '16:9']},
        'images': {'type': 'array', 'maxItems': 0},
    },
}
_EDIT_SCHEMA = {
    'type': 'object',
    'additionalProperties': False,
    'required': ['prompt', 'images'],
    'properties': {
        'prompt': {'type': 'string', 'minLength': 1, 'maxLength': 32000},
        'aspect_ratio': {'type': 'string', 'enum': ['1:1', '2:3', '3:2', '3:4', '4:3', '9:16', '16:9']},
        'images': {
            'type': 'array',
            'minItems': 1,
            'maxItems': 6,
            'items': {
                'type': 'object',
                'additionalProperties': False,
                'required': ['mime_type', 'data'],
                'properties': {
                    'mime_type': {'type': 'string', 'enum': ['image/png', 'image/jpeg', 'image/webp']},
                    'data': {'type': 'string', 'minLength': 1},
                },
            },
        },
    },
}

DEFAULT_IMAGE_CAPABILITIES = [
    {
        'capability_id': 'image.generate.nano_banana_2_1',
        'type': 'image',
        'model_id': 'gemini-nano-banana-2.1',
        'display_name': 'Nano Banana 2.1',
        'model_logo': 'nano-banana.png',
        'model_logo_mime': 'image/png',
        'operation': 'image.generate',
        'execution_state': 'enabled',
        'card_group': 'nano-banana-2-1',
        'card_title': 'Nano Banana 2.1',
        'input_summary': '提示词',
        'output_summary': 'PNG 图像 · $0.0336/张',
        'parameter_schema': _GENERATE_SCHEMA,
    },
    {
        'capability_id': 'image.edit.nano_banana_2_1',
        'type': 'image',
        'model_id': 'gemini-nano-banana-2.1',
        'display_name': 'Nano Banana 2.1',
        'model_logo': 'nano-banana.png',
        'model_logo_mime': 'image/png',
        'operation': 'image.edit',
        'execution_state': 'enabled',
        'card_group': 'nano-banana-2-1',
        'card_title': 'Nano Banana 2.1',
        'input_summary': '提示词 · 1–6 张参考图（0 张蒙版）',
        'output_summary': 'PNG 图像 · $0.0336/张',
        'parameter_schema': _EDIT_SCHEMA,
    },
    {
        'capability_id': 'image.generate.nano_banana_pro',
        'type': 'image',
        'model_id': 'gemini-3-pro-image',
        'display_name': 'Nano Banana Pro',
        'model_logo': 'nano-banana.png',
        'model_logo_mime': 'image/png',
        'operation': 'image.generate',
        'execution_state': 'enabled',
        'card_group': 'nano-banana-pro',
        'card_title': 'Nano Banana Pro',
        'input_summary': '提示词',
        'output_summary': 'PNG 图像 · $0.134/张',
        'parameter_schema': _GENERATE_SCHEMA,
    },
    {
        'capability_id': 'image.edit.nano_banana_pro',
        'type': 'image',
        'model_id': 'gemini-3-pro-image',
        'display_name': 'Nano Banana Pro',
        'model_logo': 'nano-banana.png',
        'model_logo_mime': 'image/png',
        'operation': 'image.edit',
        'execution_state': 'enabled',
        'card_group': 'nano-banana-pro',
        'card_title': 'Nano Banana Pro',
        'input_summary': '提示词 · 1–6 张参考图（0 张蒙版）',
        'output_summary': 'PNG 图像 · $0.134/张',
        'parameter_schema': _EDIT_SCHEMA,
    },
    {
        'capability_id': 'image.generate.nano_banana_2',
        'type': 'image',
        'model_id': 'gemini-3.1-flash-image',
        'display_name': 'Nano Banana 2',
        'model_logo': 'nano-banana.png',
        'model_logo_mime': 'image/png',
        'operation': 'image.generate',
        'execution_state': 'enabled',
        'card_group': 'nano-banana-2',
        'card_title': 'Nano Banana 2',
        'input_summary': '提示词',
        'output_summary': 'PNG 图像 · $0.0672/张',
        'parameter_schema': _GENERATE_SCHEMA,
    },
    {
        'capability_id': 'image.edit.nano_banana_2',
        'type': 'image',
        'model_id': 'gemini-3.1-flash-image',
        'display_name': 'Nano Banana 2',
        'model_logo': 'nano-banana.png',
        'model_logo_mime': 'image/png',
        'operation': 'image.edit',
        'execution_state': 'enabled',
        'card_group': 'nano-banana-2',
        'card_title': 'Nano Banana 2',
        'input_summary': '提示词 · 1–6 张参考图（0 张蒙版）',
        'output_summary': 'PNG 图像 · $0.0672/张',
        'parameter_schema': _EDIT_SCHEMA,
    },
    {
        'capability_id': 'image.generate.nano_banana_2_lite',
        'type': 'image',
        'model_id': 'gemini-3.1-flash-lite-image',
        'display_name': 'Nano Banana 2 Lite',
        'model_logo': 'nano-banana.png',
        'model_logo_mime': 'image/png',
        'operation': 'image.generate',
        'execution_state': 'enabled',
        'card_group': 'nano-banana-2-lite',
        'card_title': 'Nano Banana 2 Lite',
        'input_summary': '提示词',
        'output_summary': 'PNG 图像 · $0.0336/张',
        'parameter_schema': _GENERATE_SCHEMA,
    },
    {
        'capability_id': 'image.edit.nano_banana_2_lite',
        'type': 'image',
        'model_id': 'gemini-3.1-flash-lite-image',
        'display_name': 'Nano Banana 2 Lite',
        'model_logo': 'nano-banana.png',
        'model_logo_mime': 'image/png',
        'operation': 'image.edit',
        'execution_state': 'enabled',
        'card_group': 'nano-banana-2-lite',
        'card_title': 'Nano Banana 2 Lite',
        'input_summary': '提示词 · 1–6 张参考图（0 张蒙版）',
        'output_summary': 'PNG 图像 · $0.0336/张',
        'parameter_schema': _EDIT_SCHEMA,
    },
]


def _load_document(path):
    raw = Path(path).read_bytes()
    if len(raw) > 262144:
        raise ValueError('invalid_channel_metadata')
    document = yaml.safe_load(raw)
    if (not isinstance(document, dict) or document.get('schema_version') != 1
            or document.get('channel_id') != 'google' or not isinstance(document.get('capabilities'), list)):
        raise ValueError('invalid_channel_metadata')
    return document


def analysis_capabilities(path):
    if not path:
        return []
    document = _load_document(path)
    result, seen = [], set()
    for item in document['capabilities']:
        if not isinstance(item, dict):
            raise ValueError('invalid_channel_metadata')
        if item.get('capability_id') != 'analysis.analyze':
            continue
        model = item.get('model_id')
        if (model not in MODELS or model in seen or item.get('type') != 'analysis'
                or item.get('operation') != 'analysis.analyze'
                or item.get('execution_state') not in {'enabled', 'disabled', 'adapter_connected_unverified'}
                or not isinstance(item.get('parameter_schema'), dict)):
            raise ValueError('invalid_analysis_metadata')
        seen.add(model)
        result.append(item)
    return result


def image_capabilities(path):
    if not path:
        return [dict(item) for item in DEFAULT_IMAGE_CAPABILITIES]
    document = _load_document(path)
    result, seen = [], set()
    for item in document['capabilities']:
        if not isinstance(item, dict):
            raise ValueError('invalid_channel_metadata')
        if item.get('type') != 'image':
            continue
        model = item.get('model_id')
        operation = item.get('operation')
        cap_id = item.get('capability_id')
        key = (model, operation)
        if (model not in IMAGE_MODELS or key in seen
                or not isinstance(cap_id, str) or not cap_id.strip()
                or operation not in {'image.generate', 'image.edit'}
                or item.get('execution_state') not in {'enabled', 'disabled', 'adapter_connected_unverified'}
                or not isinstance(item.get('parameter_schema'), dict)):
            raise ValueError('invalid_image_metadata')
        display_name = item.get('display_name')
        if display_name is not None and (not isinstance(display_name, str) or not display_name.strip()):
            raise ValueError('invalid_image_metadata')
        seen.add(key)
        result.append(item)
    return result if result else [dict(item) for item in DEFAULT_IMAGE_CAPABILITIES]


def catalog_version(items):
    serialized = json.dumps(list(items), sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
    return 'sha256:' + hashlib.sha256(serialized).hexdigest()


def video_capabilities(path):
    """Publish only installed video declarations, with provider retirement applied."""
    if not path:
        return []
    document = _load_document(path)
    result, seen = [], set()
    for item in document['capabilities']:
        if not isinstance(item, dict):
            raise ValueError('invalid_channel_metadata')
        if item.get('type') != 'video':
            continue
        model = item.get('model_id')
        operation = item.get('operation')
        key = (model, operation)
        if (model not in VIDEO_MODELS or key in seen
                or not isinstance(item.get('capability_id'), str) or not item['capability_id'].strip()
                or operation != 'video.generate'
                or item.get('execution_state') not in {'enabled', 'disabled', 'catalog_only', 'adapter_connected_unverified'}
                or not isinstance(item.get('parameter_schema'), dict)
                or not isinstance(item.get('display_name'), str) or not item['display_name'].strip()):
            raise ValueError('invalid_video_metadata')
        seen.add(key)
        published = dict(item)
        if model in RETIRED_VIDEO_MODELS:
            published.update(execution_state='disabled', reason_code='model_retired',
                             retired_on=RETIREMENT_DATE, source_url=RETIREMENT_SOURCE)
        result.append(published)
    return result
