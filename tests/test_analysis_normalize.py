"""Unit checks for deterministic canonicalization across all World analysis stages."""
import json
from pathlib import Path
import unittest

from google_channel.analysis_normalize import compact_prompt, compact_source_text, detect_stage, normalize_output
from google_channel.analysis_schema import matches_schema, validate_schema


class AnalysisNormalizeTest(unittest.TestCase):
    def test_non_world_operations_are_untouched(self):
        value = {'prompt': 'Describe the source.', 'source_text': 'some text'}
        self.assertIsNone(detect_stage('analysis-task', value))
        self.assertEqual(compact_source_text('analysis-task', value), 'some text')
        self.assertEqual(compact_prompt('analysis-task', value), 'Describe the source.')
        raw = {'observations': ['a'], 'coverage': 'needs verification'}
        self.assertEqual(normalize_output('analysis-task', value, raw), raw)

    def test_visual_normalization_clamps_visible_text_and_fills_required_dimensions(self):
        schema = {
            'type': 'object',
            'properties': {
                'start_ms': {'type': 'integer', 'enum': [180000]},
                'end_ms': {'type': 'integer', 'enum': [225000]},
                'shots': {'type': 'array'},
                'entities': {'type': 'array'},
                'findings': {'type': 'array'},
                'uncertainties': {'type': 'array'},
            },
        }
        value = {
            'prompt': 'Analyze visual segment.',
            'video_range': {'start_ms': 179000, 'end_ms': 226000},
            'response_schema': schema,
        }
        raw = {
            'start_ms': 180000,
            'end_ms': 225000,
            'entities': [
                {'key': 'stick', 'kind': 'prop', 'name': '木棍', 'description': '角色手持木棍', 'basis': 'observed'},
            ],
            'shots': [
                {
                    'start_ms': 180000,
                    'end_ms': 220000,
                    'key': 's1',
                    'continues_before': True,
                    'continues_after': True,
                    'composition': '中景',
                    'camera_motion': '固定',
                    'subject_motion': '挥动木棍',
                    'graphic_motion': '字幕',
                    'lighting': '自然光',
                    'entity_keys': ['stick', 'missing_entity'],
                    'beats': [],
                    'visible_text': [{'start_ms': 178500, 'end_ms': 180000, 'text': '越界字幕'}],
                },
                {
                    'start_ms': 220000,
                    'end_ms': 224000,
                    'key': 's2',
                    'continues_before': True,
                    'continues_after': True,
                    'composition': '近景',
                    'camera_motion': '轻微推轨',
                    'subject_motion': '转身',
                    'graphic_motion': '字幕',
                    'lighting': '自然光',
                    'entity_keys': [],
                    'beats': [{'start_ms': 220000, 'end_ms': 224000, 'description': '转身动作'}],
                    'visible_text': [{'start_ms': 218500, 'end_ms': 220000, 'text': '边界字幕'}],
                },
            ],
            'findings': [
                {'dimension': 'prop', 'description': '木棍交互', 'shot_keys': ['s1', 'unknown'], 'basis': 'observed'},
            ],
            'uncertainties': ['全片均为后期配音，没有真实发声', '远处背景牌匾文字模糊'],
        }
        out = normalize_output('01a15b1a-d8a0-7e91-a3e4-06d3e41ca14c:visual-180000-225000', value, raw)
        self.assertEqual(out['start_ms'], 180000)
        self.assertEqual(out['end_ms'], 225000)
        self.assertEqual(out['shots'][0]['start_ms'], 180000)
        self.assertEqual(out['shots'][-1]['end_ms'], 225000)
        self.assertFalse(out['shots'][0]['continues_after'])
        self.assertFalse(out['shots'][1]['continues_before'])
        for shot in out['shots']:
            self.assertEqual(shot['beats'][0]['start_ms'], shot['start_ms'])
            self.assertEqual(shot['beats'][-1]['end_ms'], shot['end_ms'])
            for vt in shot['visible_text']:
                self.assertGreaterEqual(vt['start_ms'], shot['start_ms'])
                self.assertLessEqual(vt['end_ms'], shot['end_ms'])
                self.assertLess(vt['start_ms'], vt['end_ms'])
        self.assertEqual(len(out['findings']), 8)
        self.assertEqual(out['uncertainties'], ['远处背景牌匾文字模糊'])

    def test_audio_normalization_resolves_overlapping_layers_to_mixed(self):
        schema = {
            'type': 'object',
            'properties': {
                'start_ms': {'type': 'integer', 'enum': [60000]},
                'end_ms': {'type': 'integer', 'enum': [120000]},
                'intervals': {'type': 'array'},
                'speech': {'type': 'array'},
                'speakers': {'type': 'array'},
                'music': {'type': 'array'},
                'sound_effects': {'type': 'array'},
                'uncertainties': {'type': 'array'},
            },
        }
        value = {'prompt': 'Analyze audio.', 'response_schema': schema}
        raw = {
            'start_ms': 60000,
            'end_ms': 120000,
            'speakers': [{'key': 'sp1', 'description': '男声'}],
            'speech': [{'start_ms': 10000, 'end_ms': 30000, 'speaker_key': 'sp1', 'text': '你好', 'language': 'zho', 'intelligibility': 'clear'}],
            'music': [{'start_ms': 0, 'end_ms': 60000, 'description': '背景弦乐', 'rhythm': '中速', 'instruments': '弦乐', 'vocal': '无', 'basis': 'heard'}],
            'sound_effects': [],
            'intervals': [{'start_ms': 60000, 'end_ms': 120000, 'kind': 'speech', 'description': '错误标注为单一speech'}],
            'uncertainties': [],
        }
        out = normalize_output('01a15b1a-d8a0-7e91-a3e4-06d3e41ca14c:audio-60000-120000', value, raw)
        self.assertEqual(out['intervals'][0]['start_ms'], 60000)
        self.assertEqual(out['intervals'][-1]['end_ms'], 120000)
        kinds = [iv['kind'] for iv in out['intervals']]
        self.assertIn('mixed', kinds)
        self.assertIn('music', kinds)
        self.assertNotIn('speech', kinds)

    def test_association_prompts_and_results_normalization(self):
        assoc_src = {
            'task_id': '01a15b1a-d8a0-7e91-a3e4-06d3e41ca14c',
            'bundle_sha256': 'a' * 64,
            'source': {'sha256': 'b' * 64, 'duration_ms': 90000, 'has_audio': True},
            'nodes': [
                {'key': 'obs-' + '1' * 64, 'kind': 'entities', 'step_key': 'visual-0-45000', 'links': {}, 'value': {'key': 'c1', 'kind': 'character', 'name': '少年', 'description': '角色A', 'basis': 'observed'}},
                {'key': 'obs-' + '2' * 64, 'kind': 'entities', 'step_key': 'visual-0-45000', 'links': {}, 'value': {'key': 'p1', 'kind': 'prop', 'name': '木棍', 'description': '木棍', 'basis': 'observed'}},
                {'key': 'obs-' + '3' * 64, 'kind': 'shots', 'step_key': 'visual-0-45000', 'scope': {'start_ms': 0, 'end_ms': 45000}, 'links': {'entities': ['obs-' + '1' * 64]}, 'value': {'key': 's1'}},
                {'key': 'obs-' + '4' * 64, 'kind': 'shots', 'step_key': 'visual-45000-90000', 'scope': {'start_ms': 45000, 'end_ms': 90000}, 'links': {'entities': ['obs-' + '1' * 64]}, 'value': {'key': 's2'}},
                {'key': 'obs-' + '5' * 64, 'kind': 'speakers', 'step_key': 'audio-0-60000', 'links': {}, 'value': {'key': 'sp1', 'description': '男声'}},
            ],
            'boundaries': [
                {'left_shot': 'obs-' + '3' * 64, 'right_shot': 'obs-' + '4' * 64, 'at_ms': 45000, 'left_continues': True, 'right_continues': True, 'state': 'unreviewed'},
            ],
        }
        assoc_val = {'prompt': 'Associate', 'source_text': json.dumps(assoc_src)}
        assoc_out = normalize_output('01a15b1a-d8a0-7e91-a3e4-06d3e41ca14c:association-v1', assoc_val, {})
        self.assertEqual(assoc_out['bundle_sha256'], 'a' * 64)
        self.assertEqual(len(assoc_out['entities']), 2)
        self.assertEqual(len(assoc_out['voices']), 1)
        self.assertEqual(len(assoc_out['shot_groups']), 1)
        self.assertEqual(assoc_out['boundaries'][0]['decision'], 'merge')
        self.assertEqual(len(assoc_out['voice_links']), 1)

        prompt_src = {
            'observations': assoc_src,
            'association': assoc_out,
            'batch': {
                'batch_key': 'prompts-v1-0000',
                'scopes': [{'key': 'whole', 'evidence_keys': ['obs-' + '1' * 64, 'obs-' + '2' * 64]}],
                'targets': [{'key': 'seedance_2_0', 'requested_label': 'Seedance 2.0', 'task_type': 'video_generation', 'input_mode': 'text_to_video', 'adaptation_guidance': '分镜提示词'}],
                'pairs': [{'scope_key': 'whole', 'target_key': 'seedance_2_0'}],
            },
        }
        prompt_val = {'prompt': 'Prompts', 'source_text': json.dumps(prompt_src)}
        prompt_out = normalize_output('01a15b1a-d8a0-7e91-a3e4-06d3e41ca14c:prompts-v1-0000', prompt_val, {})
        self.assertEqual(prompt_out['batch_key'], 'prompts-v1-0000')
        self.assertEqual(len(prompt_out['items']), 1)
        self.assertEqual(prompt_out['items'][0]['evidence_keys'], ['obs-' + '1' * 64, 'obs-' + '2' * 64])
        self.assertIn('{{subject_description}}', prompt_out['items'][0]['prompt_text'])

        for rtype, skey, rng in (
            ('character', 'character.c1', None),
            ('scene', 'scene.s1', None),
            ('prop', 'prop.p1', None),
            ('shot', 'shot.sg1', {'start_ms': 0, 'end_ms': 45000}),
            ('sound_cue', 'voice.v1', None),
            ('sound_cue', 'speech.sp1', {'start_ms': 1000, 'end_ms': 5000}),
            ('music_structure', 'music.m1', {'start_ms': 0, 'end_ms': 30000}),
            ('script_unit', 'whole', None),
            ('style', 'whole', None),
            ('method', 'whole', None),
        ):
            res_src = {
                'observations': assoc_src,
                'association': assoc_out,
                'batch': {
                    'batch_key': f'results-v1-{rtype}-0000',
                    'type_key': rtype,
                    'scopes': [{'key': skey, 'evidence_keys': [] if skey == 'whole' else ['obs-' + '1' * 64], 'range': rng}],
                },
            }
            res_val = {'prompt': 'Results', 'source_text': json.dumps(res_src)}
            res_out = normalize_output(f'01a15b1a-d8a0-7e91-a3e4-06d3e41ca14c:results-v1-{rtype}-0000', res_val, {})
            self.assertEqual(res_out['type_key'], rtype)
            self.assertEqual(len(res_out['items']), 1)
            self.assertEqual(res_out['items'][0]['state'], 'ready')
            self.assertTrue(len(res_out['items'][0]['evidence_keys']) >= 1)
            self.assertEqual(len(res_out['items'][0]['payloads']), 1)


if __name__ == '__main__':
    unittest.main()
