"""Versioned Gemini API estimates; partial estimates never masquerade as totals."""
from datetime import date
from decimal import Decimal

from .store import Conflict

OMNI_MODEL = 'gemini-omni-1.1-flash'
PRICE_VERSION = 'google-gemini-2026-10-27'
PRICE_SOURCE = 'https://ai.google.dev/gemini-api/docs/pricing'
PRICE_VALID_UNTIL = '2026-11-27'
INPUT_PER_MILLION = Decimal('1.50')
TEXT_PER_MILLION = Decimal('9.00')
VIDEO_PER_MILLION = Decimal('17.50')
VIDEO_TOKENS_PER_SECOND = {'720p': Decimal('5792')}

RETIRED_VIDEO_MODELS = frozenset({
    'veo-3.1-lite-generate-preview',
    'veo-3.1-fast-generate-preview',
    'veo-3.1-generate-preview',
})
RETIREMENT_DATE = '2026-10-22'
RETIREMENT_SOURCE = 'https://ai.google.dev/gemini-api/docs/deprecations'
CHECKED_ON = '2026-10-27'


def video_preflight(model_id):
    retired = model_id in RETIRED_VIDEO_MODELS
    return {
        'schema_version': 1,
        'channel_id': 'google',
        'model_id': model_id,
        'can_submit': False,
        'blocked_reason': 'model_retired' if retired else 'capability_not_supported',
        'quote': {'amount': None, 'unit': 'USD', 'source': 'unknown'},
        'model_status': {
            'state': 'retired' if retired else 'unsupported',
            'retired_on': RETIREMENT_DATE if retired else None,
            'source_url': RETIREMENT_SOURCE if retired else None,
            'checked_on': CHECKED_ON,
        },
    }


def decimal_string(value):
    return format(value.quantize(Decimal('0.00000001')).normalize(), 'f')


def estimate(payload, *, today=None):
    if (today or date.today()).isoformat() >= PRICE_VALID_UNTIL:
        raise Conflict('price_table_expired')
    resolution = payload['resolution']
    if resolution not in VIDEO_TOKENS_PER_SECOND:
        raise Conflict('price_unknown')
    tokens = VIDEO_TOKENS_PER_SECOND[resolution] * Decimal(payload['duration'][:-1])
    subtotal = tokens * VIDEO_PER_MILLION / Decimal('1000000')
    return {
        'amount': None, 'known_amount': decimal_string(subtotal), 'unit': 'USD',
        'source': 'price_table', 'price_version': PRICE_VERSION, 'source_url': PRICE_SOURCE,
        'checked_on': CHECKED_ON, 'expires_on': PRICE_VALID_UNTIL, 'complete': False,
        'components': [{'kind': 'video_output', 'estimated_tokens': decimal_string(tokens),
                        'amount': decimal_string(subtotal)}],
        'unknown_components': ['input_tokens', 'text_output_tokens', 'thought_tokens'],
    }


def safe_usage(value):
    if not isinstance(value, dict):
        return {}
    result = {}
    for key in ('total_input_tokens', 'total_output_tokens', 'total_thought_tokens',
                'total_cached_tokens', 'total_tool_use_tokens', 'total_tokens'):
        number = value.get(key)
        if type(number) is int and 0 <= number <= 10**12:
            result[key] = number
    for key in ('input_tokens_by_modality', 'output_tokens_by_modality'):
        entries = value.get(key)
        if not isinstance(entries, list) or len(entries) > 8:
            continue
        if all(isinstance(item, dict) and item.get('modality') in {'text', 'image', 'audio', 'video', 'document'}
               and type(item.get('tokens')) is int and 0 <= item['tokens'] <= 10**12 for item in entries):
            result[key] = [{'modality': item['modality'], 'tokens': item['tokens']} for item in entries]
    return result


def settlement(usage, price_version):
    """Charge exact reported token categories only when the complete bill is known."""
    normalized = safe_usage(usage)
    unknown = {'amount': None, 'unit': 'USD', 'source': 'unknown', 'price_version': price_version,
               'reason_code': 'usage_incomplete'}
    if price_version != PRICE_VERSION:
        return dict(unknown, reason_code='price_version_unknown'), normalized
    required = {'total_input_tokens', 'total_output_tokens', 'total_thought_tokens', 'output_tokens_by_modality'}
    if (not required <= normalized.keys() or normalized.get('total_tool_use_tokens', 0)
            or normalized.get('total_cached_tokens', 0)):
        return unknown, normalized
    if ('total_tokens' in normalized and normalized['total_tokens'] != normalized['total_input_tokens']
            + normalized['total_output_tokens'] + normalized['total_thought_tokens']):
        return unknown, normalized
    outputs = normalized['output_tokens_by_modality']
    if (any(item['modality'] not in {'text', 'video'} for item in outputs)
            or len({item['modality'] for item in outputs}) != len(outputs)
            or sum(item['tokens'] for item in outputs) != normalized['total_output_tokens']):
        return unknown, normalized
    total = Decimal(normalized['total_input_tokens']) * INPUT_PER_MILLION
    total += Decimal(normalized['total_thought_tokens']) * TEXT_PER_MILLION
    for item in outputs:
        total += Decimal(item['tokens']) * (VIDEO_PER_MILLION if item['modality'] == 'video' else TEXT_PER_MILLION)
    return {'amount': decimal_string(total / Decimal('1000000')), 'unit': 'USD', 'source': 'price_table',
            'price_version': price_version}, normalized
