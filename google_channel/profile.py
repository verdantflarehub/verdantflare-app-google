"""Google AI Studio connection and manual billing registration rules."""
from datetime import datetime, date
from decimal import Decimal, InvalidOperation
import os
from urllib.parse import urlsplit

from .store import now

DEFAULT_ORIGIN = 'https://generativelanguage.googleapis.com'


def origin(value):
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError('invalid_api_url')
    parsed = urlsplit(value)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in {'', '/'}):
        raise ValueError('invalid_api_url')
    try:
        port = parsed.port
    except ValueError:
        raise ValueError('invalid_api_url') from None
    canonical = 'https://' + parsed.hostname.lower() + (':' + str(port) if port not in {None, 443} else '')
    allowed = {DEFAULT_ORIGIN, *filter(None, os.environ.get('GOOGLE_ALLOWED_API_ORIGINS', '').split(','))}
    if canonical not in allowed:
        raise ValueError('api_origin_not_allowed')
    return canonical


def amount(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 32:
        raise ValueError('invalid_amount')
    try:
        number = Decimal(value)
        if not number.is_finite() or number < 0 or number > Decimal('1e15') or number.as_tuple().exponent < -8:
            raise ValueError('invalid_amount')
    except InvalidOperation:
        raise ValueError('invalid_amount') from None
    return '0' if number == 0 and number.as_tuple().exponent > 0 else format(number, 'f')


def registration(value):
    if not isinstance(value, dict) or set(value) - {
        'google_project_id', 'billing', 'tier', 'balance', 'grant_balance', 'grant_expiry',
        'grant_note', 'postpaid_used', 'budget_limit', 'observed_at', 'note'
    }:
        raise ValueError('invalid_registration')
    result = dict(value)
    if result.get('billing') not in {None, 'prepaid', 'postpaid'}:
        raise ValueError('invalid_billing')
    for field in ('balance', 'grant_balance', 'postpaid_used', 'budget_limit'):
        if result.get(field) is not None:
            result[field] = amount(result[field])
    for field in ('google_project_id', 'tier', 'grant_note', 'note'):
        if field in result and (not isinstance(result[field], str) or len(result[field]) > 2000):
            raise ValueError('invalid_registration')
    if result.get('grant_expiry') is not None:
        value = result['grant_expiry']
        if not isinstance(value, str):
            raise ValueError('invalid_grant_expiry')
        result['grant_expiry'] = date.fromisoformat(value).isoformat()
    if result.get('observed_at') is not None and not isinstance(result['observed_at'], str):
        raise ValueError('invalid_observation_time')
    if result.get('observed_at'):
        timestamp = datetime.fromisoformat(result['observed_at'])
        if timestamp.tzinfo is None:
            raise ValueError('observation_timezone_required')
    elif any(result.get(field) is not None for field in ('balance', 'grant_balance', 'postpaid_used')):
        result['observed_at'] = now()
    return result
