"""Bounded JSON Schema subset for provider output; no references or code execution."""
import hashlib
import json
import math


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def schema_digest(schema):
    return hashlib.sha256(encoded(schema)).hexdigest()


def finite_number(value):
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def validate_schema(schema):
    """Reject unsupported/ambiguous schemas before reserving a paid call."""
    try:
        if len(encoded(schema)) > 32768:
            raise ValueError('invalid_response_schema')
    except (TypeError, ValueError, RecursionError, UnicodeError):
        raise ValueError('invalid_response_schema') from None
    nodes = 0

    def visit(node, depth):
        nonlocal nodes
        nodes += 1
        if depth > 12 or nodes > 512 or not isinstance(node, dict):
            raise ValueError('invalid_response_schema')
        kind = node.get('type')
        extras = {'object': {'properties', 'required', 'additionalProperties'},
                  'array': {'items', 'minItems', 'maxItems'},
                  'string': {'minLength', 'maxLength'},
                  'integer': {'minimum', 'maximum'}, 'number': {'minimum', 'maximum'}, 'boolean': set()}
        if not isinstance(kind, str) or kind not in extras or set(node) - (extras[kind] | {'type', 'description', 'enum'}):
            raise ValueError('invalid_response_schema')
        if 'description' in node and (not isinstance(node['description'], str) or len(node['description']) > 4000):
            raise ValueError('invalid_response_schema')
        if 'enum' in node:
            values = node['enum']
            if kind in {'object', 'array'} or not isinstance(values, list) or not 1 <= len(values) <= 128:
                raise ValueError('invalid_response_schema')
            bare = {k: v for k, v in node.items() if k != 'enum'}
            if any(not matches_schema(bare, value) for value in values) or len({encoded(v) for v in values}) != len(values):
                raise ValueError('invalid_response_schema')
        pairs = {'array': ('minItems', 'maxItems'), 'string': ('minLength', 'maxLength'),
                 'integer': ('minimum', 'maximum'), 'number': ('minimum', 'maximum')}
        if kind in pairs:
            lower, upper = pairs[kind]
            for name in (lower, upper):
                if name not in node:
                    continue
                value = node[name]
                if not finite_number(value):
                    raise ValueError('invalid_response_schema')
                if kind in {'array', 'string'} and (type(value) is not int or not 0 <= value <= 1000000):
                    raise ValueError('invalid_response_schema')
            if lower in node and upper in node and node[lower] > node[upper]:
                raise ValueError('invalid_response_schema')
        if kind == 'object':
            props, required = node.get('properties'), node.get('required')
            if not isinstance(props, dict) or node.get('additionalProperties') is not False or not isinstance(required, list):
                raise ValueError('invalid_response_schema')
            if any(not isinstance(k, str) or not k or len(k) > 128 for k in props):
                raise ValueError('invalid_response_schema')
            if any(not isinstance(k, str) or k not in props for k in required) or len(set(required)) != len(required):
                raise ValueError('invalid_response_schema')
            for child in props.values():
                visit(child, depth + 1)
        elif kind == 'array':
            visit(node.get('items'), depth + 1)

    if not isinstance(schema, dict) or schema.get('type') != 'object':
        raise ValueError('invalid_response_schema')
    try:
        visit(schema, 1)
    except (TypeError, OverflowError, RecursionError):
        raise ValueError('invalid_response_schema') from None
    return schema


def matches_schema(schema, value):
    """Validate only the already-checked subset, including bool vs integer."""
    kind = schema['type']
    if kind == 'object':
        if not isinstance(value, dict) or set(value) - set(schema['properties']) or any(k not in value for k in schema['required']):
            return False
        return all(matches_schema(schema['properties'][key], item) for key, item in value.items())
    if kind == 'array':
        return (isinstance(value, list) and schema.get('minItems', 0) <= len(value) <= schema.get('maxItems', math.inf)
                and all(matches_schema(schema['items'], item) for item in value))
    if kind == 'string':
        valid = isinstance(value, str) and schema.get('minLength', 0) <= len(value) <= schema.get('maxLength', math.inf)
    elif kind in {'integer', 'number'}:
        valid = (finite_number(value)
                 and (kind == 'number' or value == int(value))
                 and schema.get('minimum', -math.inf) <= value <= schema.get('maximum', math.inf))
    else:
        valid = type(value) is bool
    return valid and ('enum' not in schema or any(type(value) is type(v) and value == v
                 or kind in {'integer', 'number'} and type(v) in (int, float) and value == v for v in schema['enum']))
