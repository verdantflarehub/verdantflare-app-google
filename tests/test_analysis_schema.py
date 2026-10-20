"""Contract checks for the small structured-output grammar, without a provider."""
import copy
import unittest

from google_channel.analysis_schema import matches_schema, schema_digest, validate_schema


def obj(properties):
    return {'type': 'object', 'properties': properties, 'required': list(properties), 'additionalProperties': False}


class OutputSchemaTest(unittest.TestCase):
    def setUp(self):
        self.schema = obj({'shots': {'type': 'array', 'minItems': 1, 'maxItems': 3,
            'items': obj({'start_ms': {'type': 'integer', 'minimum': 0, 'maximum': 60000},
                          'motion': {'type': 'string', 'minLength': 1, 'maxLength': 80},
                          'observed': {'type': 'boolean'}})}})
        self.document = {'shots': [{'start_ms': 0, 'motion': '固定镜头', 'observed': True}]}

    def test_nested_valid_output_and_canonical_digest(self):
        self.assertEqual(validate_schema(self.schema), self.schema)
        self.assertTrue(matches_schema(self.schema, self.document))
        self.assertEqual(schema_digest(self.schema), schema_digest(dict(reversed(list(self.schema.items())))))

    def test_wrong_output_is_rejected_including_boolean_numeric_confusion(self):
        for value in ({}, {'shots': []}, {'shots': self.document['shots'] * 4}, {**self.document, 'extra': 1},
                      {'shots': [{'start_ms': True, 'motion': 'fixed', 'observed': True}]},
                      {'shots': [{'start_ms': 10**400, 'motion': 'fixed', 'observed': True}]},
                      {'shots': [{'start_ms': -1, 'motion': 'fixed', 'observed': True}]},
                      {'shots': [{'start_ms': 10.5, 'motion': 'fixed', 'observed': True}]},
                      {'shots': [{'start_ms': 0, 'motion': '', 'observed': True}]},
                      {'shots': [{'start_ms': 0, 'motion': 'fixed', 'observed': 1}]}):
            self.assertFalse(matches_schema(self.schema, value), value)

    def test_integer_schema_accepts_integral_json_number(self):
        self.document['shots'][0]['start_ms'] = 1.0
        self.assertTrue(matches_schema(self.schema, self.document))

    def test_unsupported_or_ill_typed_schema_never_reaches_provider(self):
        for value in (None, [], {'type': 'string'}, {**self.schema, '$ref': 'https://example.invalid/schema'},
                      {**self.schema, 'required': ['missing']}, {**self.schema, 'required': ['shots', 'shots']},
                      {**self.schema, 'additionalProperties': True}, obj({'v': {'type': 'array'}}),
                      obj({'v': {'type': 'number', 'minimum': False}}),
                      obj({'v': {'type': 'string', 'pattern': '.*'}}),
                      obj({'v': {'type': 'number', 'minimum': 10**400}}),
                      obj({'v': {'type': 'string', 'minLength': 4, 'maxLength': 2}}),
                      obj({'v': {'type': 'string', 'minLength': 1.5}}),
                      obj({'v': {'type': 'array', 'items': {'type': 'boolean'}, 'maxItems': -1}}),
                      obj({'v': {'type': ['string', 'null']}})):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_schema(value)

    def test_bounded_depth_size_and_node_count(self):
        value = {'type': 'string'}
        for _ in range(13):
            value = obj({'child': value})
        wide = obj({str(i): {'type': 'string'} for i in range(512)})
        long = obj({'v': {'type': 'string', 'description': '中' * 4001}})
        for schema in (value, wide, long, {**self.schema, 'description': 'x' * 32769}):
            with self.assertRaises(ValueError):
                validate_schema(schema)

    def test_enum_types_values_and_bounds(self):
        valid = obj({'v': {'type': 'string', 'enum': ['complete', 'partial']}})
        validate_schema(valid)
        self.assertTrue(matches_schema(valid, {'v': 'partial'}))
        self.assertFalse(matches_schema(valid, {'v': 'invented'}))
        for node in ({'type': 'integer', 'enum': [True]}, {'type': 'string', 'enum': []},
                     {'type': 'string', 'enum': ['same', 'same']},
                     {'type': 'integer', 'minimum': 5, 'enum': [3]},
                     {'type': 'object', 'enum': [{}], 'properties': {}, 'required': [], 'additionalProperties': False}):
            with self.assertRaises(ValueError):
                validate_schema(obj({'v': node}))
