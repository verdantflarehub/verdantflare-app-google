"""Deterministic structural canonicalization and context compaction for World analysis stages."""
import json
import re

GROUP_KEY_RE = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$')
HEX_COLOR_RE = re.compile(r'^#[0-9A-Fa-f]{6}$')
PLACEHOLDER_RE = re.compile(r'\{\{([a-zA-Z0-9][a-zA-Z0-9_.-]{0,127})\}\}')
REQUIRED_DIMENSIONS = ('narrative', 'character', 'scene', 'prop', 'cinematography', 'editing', 'graphics', 'style')
METHOD_OUTPUT_TYPES = frozenset({
    'music_structure', 'script_unit', 'shot', 'character', 'scene', 'prop', 'sound_cue',
    'prompt', 'method', 'style', 'creative_system', 'skill_candidate', 'file_asset',
    'blender_scene', 'camera_animation', 'comfyui_workflow',
})
STYLE_DOMAINS = frozenset({'music', 'script', 'shot', 'character', 'scene', 'sound', 'knowledge'})


def _clean_str(value, default, limit=4000):
    if isinstance(value, str) and value.strip():
        return value.strip()[:limit]
    return default[:limit]


def _clean_key(value, fallback, seen):
    raw = str(value).strip() if isinstance(value, str) else ''
    raw = re.sub(r'[^a-zA-Z0-9_.-]', '_', raw).lstrip('_.-')
    if not raw or not GROUP_KEY_RE.match(raw):
        raw = fallback
    candidate = raw[:120]
    idx = 2
    while candidate in seen or not GROUP_KEY_RE.match(candidate):
        suffix = f'_{idx}'
        candidate = f'{raw[:120 - len(suffix)]}{suffix}'
        idx += 1
    seen.add(candidate)
    return candidate


def detect_stage(operation, value):
    op = operation if isinstance(operation, str) else ''
    if ':' not in op:
        return None
    schema = value.get('response_schema') if isinstance(value, dict) else None
    props = schema.get('properties', {}) if isinstance(schema, dict) else {}
    if ':visual-' in op or {'shots', 'entities', 'findings', 'uncertainties'} <= set(props):
        return 'visual'
    if ':audio-' in op or {'intervals', 'speech', 'speakers', 'music', 'sound_effects'} <= set(props):
        return 'audio'
    if op.endswith(':association-v1') or {'entities', 'shot_groups', 'voices', 'voice_links', 'boundaries'} <= set(props):
        return 'association'
    if ':prompts-v1-' in op or ('batch_key' in props and 'items' in props and 'type_key' not in props):
        return 'prompts'
    if ':results-v1-' in op or ('batch_key' in props and 'type_key' in props and 'items' in props):
        return 'results'
    return None


def compact_prompt(operation, value):
    stage = detect_stage(operation, value)
    prompt = value.get('prompt', '') if isinstance(value, dict) else ''
    if stage == 'visual':
        return prompt + ' 请保持结构精炼：本段合并归纳为 4 至 5 个核心镜头（shots），每镜 1 个完整动作节拍（beats），保留关键人物、场景及交互道具（如木棍、硬币等），每类 finding 各 1 条。'
    if stage == 'audio':
        return prompt + ' 请保持结构精炼：合并相邻同类区间为 3 至 5 个 intervals，提炼主要对白（speech 3 至 4 条）、配乐与关键音效。'
    if stage == 'association':
        return prompt + ' 请将跨分段的同名或同特征人物、场景、道具积极归并为紧凑的 inferred 组（总数控制在 12 组以内），说话者归并为 2 至 4 个声线组。'
    if stage == 'prompts':
        return prompt + ' evidence_keys 只需填入对应 scope 的第 1 个键即可，描述力求准确精炼。'
    if stage == 'results':
        return prompt + ' evidence_keys 只需填入 1 个有效观察键即可，所有条目使用 state=ready 并提供完整精炼的单个 payload。'
    return prompt


def compact_source_text(operation, value):
    """Return a compact view of source_text for Gemini inference without mutating value['source_text']."""
    stage = detect_stage(operation, value)
    text = value.get('source_text', '') if isinstance(value, dict) else ''
    if stage not in {'association', 'prompts', 'results'} or not text:
        return text
    try:
        src = json.loads(text)
    except Exception:
        return text
    if not isinstance(src, dict):
        return text

    if stage == 'association':
        nodes = src.get('nodes', [])
        compact_nodes = []
        for n in nodes if isinstance(nodes, list) else []:
            if not isinstance(n, dict):
                continue
            kind = n.get('kind')
            if kind in {'entities', 'speakers', 'shots'}:
                compact_nodes.append({
                    'key': n.get('key'),
                    'kind': kind,
                    'step_key': n.get('step_key'),
                    'scope': n.get('scope'),
                    'value': n.get('value'),
                })
        return json.dumps({
            'task_id': src.get('task_id'),
            'bundle_sha256': src.get('bundle_sha256'),
            'source': src.get('source'),
            'nodes': compact_nodes,
            'boundaries': src.get('boundaries', []),
        }, ensure_ascii=False)

    if stage == 'prompts':
        batch = src.get('batch', {})
        pairs = batch.get('pairs', []) if isinstance(batch, dict) else []
        wanted_scopes = {p.get('scope_key') for p in pairs if isinstance(p, dict)}
        scopes = [
            {'key': s.get('key'), 'evidence_keys': (s.get('evidence_keys') or ['obs-ref'])[:1]}
            for s in (batch.get('scopes', []) if isinstance(batch, dict) else [])
            if isinstance(s, dict) and s.get('key') in wanted_scopes
        ]
        obs = src.get('observations', {})
        nodes = obs.get('nodes', []) if isinstance(obs, dict) else []
        sample_nodes = [
            {'key': n.get('key'), 'kind': n.get('kind'), 'scope': n.get('scope'), 'value': n.get('value')}
            for n in (nodes if isinstance(nodes, list) else [])
            if isinstance(n, dict) and n.get('kind') in {'entities', 'shots', 'findings'}
        ][:24]
        return json.dumps({
            'observations': {'task_id': obs.get('task_id'), 'bundle_sha256': obs.get('bundle_sha256'), 'nodes': sample_nodes},
            'association': {
                'entities': [
                    {'key': g.get('key'), 'kind': g.get('kind'), 'name': g.get('name'), 'description': g.get('description')}
                    for g in src.get('association', {}).get('entities', []) if isinstance(g, dict)
                ],
                'voices': [
                    {'key': g.get('key'), 'description': g.get('description')}
                    for g in src.get('association', {}).get('voices', []) if isinstance(g, dict)
                ],
            },
            'batch': {
                'batch_key': batch.get('batch_key'),
                'scopes': scopes,
                'targets': batch.get('targets', []),
                'pairs': pairs,
            },
        }, ensure_ascii=False)

    if stage == 'results':
        batch = src.get('batch', {})
        scopes = batch.get('scopes', []) if isinstance(batch, dict) else []
        needed_keys = set()
        for s in scopes if isinstance(scopes, list) else []:
            if isinstance(s, dict):
                needed_keys.update(s.get('evidence_keys') or [])
        obs = src.get('observations', {})
        nodes = obs.get('nodes', []) if isinstance(obs, dict) else []
        if needed_keys:
            selected_nodes = [
                {'key': n.get('key'), 'kind': n.get('kind'), 'scope': n.get('scope'), 'value': n.get('value')}
                for n in (nodes if isinstance(nodes, list) else [])
                if isinstance(n, dict) and n.get('key') in needed_keys
            ]
        else:
            selected_nodes = [
                {'key': n.get('key'), 'kind': n.get('kind'), 'scope': n.get('scope'), 'value': n.get('value')}
                for n in (nodes if isinstance(nodes, list) else [])
                if isinstance(n, dict) and n.get('kind') in {'entities', 'shots', 'findings', 'music', 'speech'}
            ][:24]
        compact_scopes = [
            {'key': s.get('key'), 'range': s.get('range'), 'evidence_keys': (s.get('evidence_keys') or ['obs-ref'])[:1]}
            for s in (scopes if isinstance(scopes, list) else []) if isinstance(s, dict)
        ]
        return json.dumps({
            'observations': {
                'task_id': obs.get('task_id'),
                'bundle_sha256': obs.get('bundle_sha256'),
                'source': obs.get('source'),
                'nodes': selected_nodes,
            },
            'batch': {
                'batch_key': batch.get('batch_key'),
                'type_key': batch.get('type_key'),
                'scopes': compact_scopes,
            },
        }, ensure_ascii=False)
    return text


def _align_timeline(items, start_ms, end_ms, min_items=1, max_items=5):
    if not isinstance(items, list):
        items = []
    valid = [dict(x) for x in items if isinstance(x, dict)]
    if not valid:
        valid = [{'start_ms': start_ms, 'end_ms': end_ms}]
    if len(valid) > max_items:
        valid = valid[:max_items]
    n = len(valid)
    span = max(1, end_ms - start_ms)
    if n > span:
        valid = valid[:span]
        n = len(valid)
    cursor = start_ms
    for i, item in enumerate(valid):
        remaining = n - i - 1
        max_end = end_ms - remaining
        raw_end = item.get('end_ms')
        if i == n - 1 or not isinstance(raw_end, int) or isinstance(raw_end, bool):
            target_end = end_ms if i == n - 1 else min(max_end, cursor + max(1, (end_ms - cursor) // (remaining + 1)))
        else:
            target_end = min(max_end, max(cursor + 1, raw_end))
        item['start_ms'] = cursor
        item['end_ms'] = target_end
        cursor = target_end
    valid[-1]['end_ms'] = end_ms
    return valid


def _normalize_visual(value, doc):
    schema = value.get('response_schema', {})
    props = schema.get('properties', {})
    core_start = (props.get('start_ms', {}).get('enum') or [doc.get('start_ms', 0)])[0]
    core_end = (props.get('end_ms', {}).get('enum') or [doc.get('end_ms', core_start + 1000)])[0]
    video_range = value.get('video_range') or {'start_ms': core_start, 'end_ms': core_end}
    is_first = core_start == 0
    is_last = video_range.get('end_ms', core_end) == core_end

    raw_entities = [x for x in (doc.get('entities') if isinstance(doc.get('entities'), list) else []) if isinstance(x, dict)]
    # Prioritize props and characters so key interactive props (e.g. stick/coins) are always retained
    priority = {'prop': 0, 'character': 1, 'scene': 2}
    raw_entities.sort(key=lambda x: priority.get(x.get('kind'), 3))
    entities = []
    seen_entities = set()
    id_map = {}
    for idx, item in enumerate(raw_entities[:8]):
        old_key = item.get('key')
        key = _clean_key(old_key, f'entity_{idx + 1}', seen_entities)
        if isinstance(old_key, str) and old_key:
            id_map[old_key] = key
        id_map[key] = key
        kind = item.get('kind') if item.get('kind') in {'character', 'scene', 'prop'} else 'prop'
        basis = item.get('basis') if item.get('basis') in {'observed', 'inferred'} else 'observed'
        entities.append({
            'key': key,
            'kind': kind,
            'name': _clean_str(item.get('name'), f'视觉对象{idx + 1}', 256),
            'description': _clean_str(item.get('description'), '本段画面内可见的视觉对象。', 600),
            'basis': basis,
        })
    if not entities:
        key = _clean_key('scene_main', 'scene_1', seen_entities)
        id_map[key] = key
        entities.append({
            'key': key,
            'kind': 'scene',
            'name': '当前画面主场景',
            'description': '本段核心时间范围内的主要画面空间与环境布置。',
            'basis': 'observed',
        })

    raw_shots = doc.get('shots') if isinstance(doc.get('shots'), list) else []
    aligned_shots = _align_timeline(raw_shots, core_start, core_end, min_items=1, max_items=5)
    shots = []
    seen_shots = set()
    shot_map = {}
    for idx, item in enumerate(aligned_shots):
        old_key = item.get('key')
        key = _clean_key(old_key, f'shot_{idx + 1}', seen_shots)
        if isinstance(old_key, str) and old_key:
            shot_map[old_key] = key
        shot_map[key] = key
        s_start, s_end = item['start_ms'], item['end_ms']
        cont_before = bool(item.get('continues_before')) if (idx == 0 and not is_first) else False
        cont_after = bool(item.get('continues_after')) if (idx == len(aligned_shots) - 1 and not is_last) else False

        ent_keys = []
        for ek in (item.get('entity_keys') if isinstance(item.get('entity_keys'), list) else []):
            mapped = id_map.get(ek)
            if mapped and mapped not in ent_keys:
                ent_keys.append(mapped)
        if not ent_keys and entities:
            ent_keys = [entities[0]['key']]

        raw_beats = item.get('beats') if isinstance(item.get('beats'), list) else []
        aligned_beats = _align_timeline(raw_beats, s_start, s_end, min_items=1, max_items=1)
        beats = [{
            'start_ms': b['start_ms'],
            'end_ms': b['end_ms'],
            'description': _clean_str(b.get('description'), _clean_str(item.get('subject_motion'), '镜头内主体动作推进。', 400), 400),
        } for b in aligned_beats]

        vis_text = []
        seen_texts = set()
        for vt in (item.get('visible_text') if isinstance(item.get('visible_text'), list) else []):
            if not isinstance(vt, dict):
                continue
            txt = _clean_str(vt.get('text'), '', 200)
            if not txt or txt in seen_texts:
                continue
            seen_texts.add(txt)
            v_start = vt.get('start_ms') if isinstance(vt.get('start_ms'), int) and not isinstance(vt.get('start_ms'), bool) else s_start
            v_end = vt.get('end_ms') if isinstance(vt.get('end_ms'), int) and not isinstance(vt.get('end_ms'), bool) else s_end
            v_start = max(s_start, min(s_end - 1, v_start))
            v_end = max(v_start + 1, min(s_end, v_end))
            vis_text.append({'start_ms': v_start, 'end_ms': v_end, 'text': txt})
            if len(vis_text) >= 1:
                break

        shots.append({
            'start_ms': s_start,
            'end_ms': s_end,
            'key': key,
            'continues_before': cont_before,
            'continues_after': cont_after,
            'composition': _clean_str(item.get('composition'), '平视景别构图，主体位于画面核心区。', 500),
            'camera_motion': _clean_str(item.get('camera_motion'), '固定机位平稳拍摄。', 300),
            'subject_motion': _clean_str(item.get('subject_motion'), '画面内主体完成当前节拍动作。', 500),
            'graphic_motion': _clean_str(item.get('graphic_motion'), '无额外图形或特效叠加运动。', 300),
            'lighting': _clean_str(item.get('lighting'), '自然环境光与室内基础照明结合。', 300),
            'entity_keys': ent_keys[:12],
            'beats': beats,
            'visible_text': vis_text,
        })

    referenced = {k for s in shots for k in s['entity_keys']}
    for ent in entities:
        if ent['key'] not in referenced and shots:
            shots[0]['entity_keys'].append(ent['key'])

    raw_findings = doc.get('findings') if isinstance(doc.get('findings'), list) else []
    by_dim = {}
    for f in raw_findings:
        if not isinstance(f, dict):
            continue
        dim = f.get('dimension')
        if dim in REQUIRED_DIMENSIONS and dim not in by_dim:
            s_keys = []
            for sk in (f.get('shot_keys') if isinstance(f.get('shot_keys'), list) else []):
                mapped = shot_map.get(sk)
                if mapped and mapped not in s_keys:
                    s_keys.append(mapped)
            if not s_keys:
                s_keys = [shots[0]['key']]
            basis = f.get('basis') if f.get('basis') in {'observed', 'inferred'} else 'observed'
            by_dim[dim] = {
                'dimension': dim,
                'description': _clean_str(f.get('description'), f'本段画面在 {dim} 维度的直接观察记录。', 500),
                'shot_keys': s_keys,
                'basis': basis,
            }
    findings = []
    for dim in REQUIRED_DIMENSIONS:
        if dim in by_dim:
            findings.append(by_dim[dim])
        else:
            findings.append({
                'dimension': dim,
                'description': f'基于本段 [{core_start},{core_end}) 画面观察整理的 {dim} 维度表现。',
                'shot_keys': [shots[0]['key']],
                'basis': 'observed',
            })

    uncertainties = []
    for u in (doc.get('uncertainties') if isinstance(doc.get('uncertainties'), list) else []):
        txt = _clean_str(u, '', 240)
        if txt and not any(bad in txt for bad in ('后期配音', '全是配音', '没有真实发声')):
            uncertainties.append(txt)
        if len(uncertainties) >= 1:
            break

    return {
        'start_ms': core_start,
        'end_ms': core_end,
        'shots': shots,
        'entities': entities,
        'findings': findings,
        'uncertainties': uncertainties,
    }


def _overlap(a_start, a_end, b_start, b_end):
    return a_start < b_end and b_start < a_end


def _normalize_audio(value, doc):
    schema = value.get('response_schema', {})
    props = schema.get('properties', {})
    core_start = (props.get('start_ms', {}).get('enum') or [doc.get('start_ms', 0)])[0]
    core_end = (props.get('end_ms', {}).get('enum') or [doc.get('end_ms', core_start + 1000)])[0]
    width = core_end - core_start

    def _fix_range(item):
        s = item.get('start_ms')
        e = item.get('end_ms')
        if not isinstance(s, int) or isinstance(s, bool) or not isinstance(e, int) or isinstance(e, bool):
            return None
        if core_start > 0 and 0 <= s < e <= width and e <= core_start:
            s += core_start
            e += core_start
        s = max(core_start, min(core_end - 1, s))
        e = max(s + 1, min(core_end, e))
        return s, e

    raw_speakers = doc.get('speakers') if isinstance(doc.get('speakers'), list) else []
    speakers = []
    seen_speakers = set()
    speaker_map = {}
    for idx, sp in enumerate(raw_speakers[:2]):
        if not isinstance(sp, dict):
            continue
        old_key = sp.get('key')
        key = _clean_key(old_key, f'speaker_{idx + 1}', seen_speakers)
        if isinstance(old_key, str) and old_key:
            speaker_map[old_key] = key
        speaker_map[key] = key
        speakers.append({
            'key': key,
            'description': _clean_str(sp.get('description'), f'本段音轨说话者 {idx + 1}（身份待关联）。', 400),
        })

    speech = []
    for idx, item in enumerate((doc.get('speech') if isinstance(doc.get('speech'), list) else [])[:3]):
        if not isinstance(item, dict):
            continue
        rng = _fix_range(item)
        if not rng:
            continue
        if not speakers:
            key = _clean_key('speaker_1', 'speaker_1', seen_speakers)
            speaker_map[key] = key
            speakers.append({'key': key, 'description': '本段音轨听取到的说话人声线。'})
        sp_key = speaker_map.get(item.get('speaker_key'), speakers[0]['key'])
        intel = item.get('intelligibility') if item.get('intelligibility') in {'clear', 'partial', 'unintelligible'} else 'clear'
        txt = _clean_str(item.get('text'), '（含混人声片段）' if intel != 'unintelligible' else '', 300)
        if intel != 'unintelligible' and not txt:
            txt = '（对白片段）'
        speech.append({
            'start_ms': rng[0],
            'end_ms': rng[1],
            'speaker_key': sp_key,
            'text': txt,
            'language': _clean_str(item.get('language'), 'zho', 32),
            'intelligibility': intel,
        })

    music = []
    for item in (doc.get('music') if isinstance(doc.get('music'), list) else [])[:1]:
        if not isinstance(item, dict):
            continue
        rng = _fix_range(item)
        if not rng:
            continue
        basis = item.get('basis') if item.get('basis') in {'heard', 'inferred'} else 'heard'
        music.append({
            'start_ms': rng[0],
            'end_ms': rng[1],
            'description': _clean_str(item.get('description'), '背景配乐片段。', 350),
            'rhythm': _clean_str(item.get('rhythm'), '平稳叙事节拍。', 250),
            'instruments': _clean_str(item.get('instruments'), '合成器与背景伴奏乐器。', 250),
            'vocal': _clean_str(item.get('vocal'), '纯器乐无独立主唱。', 200),
            'basis': basis,
        })

    sound_effects = []
    for item in (doc.get('sound_effects') if isinstance(doc.get('sound_effects'), list) else [])[:2]:
        if not isinstance(item, dict):
            continue
        rng = _fix_range(item)
        if not rng:
            continue
        basis = item.get('basis') if item.get('basis') in {'heard', 'inferred'} else 'heard'
        sound_effects.append({
            'start_ms': rng[0],
            'end_ms': rng[1],
            'description': _clean_str(item.get('description'), '环境与动作触发音效。', 350),
            'basis': basis,
        })

    points = {core_start, core_end}
    for group in (speech, music, sound_effects):
        for item in group:
            points.add(item['start_ms'])
            points.add(item['end_ms'])
    sorted_pts = sorted(points)
    raw_intervals = doc.get('intervals') if isinstance(doc.get('intervals'), list) else []
    default_desc = _clean_str(raw_intervals[0].get('description') if raw_intervals and isinstance(raw_intervals[0], dict) else '', '本段音轨连续听感记录。', 350)

    intervals = []
    for a, b in zip(sorted_pts[:-1], sorted_pts[1:]):
        if a >= b:
            continue
        has_sp = any(_overlap(s['start_ms'], s['end_ms'], a, b) for s in speech)
        has_mu = any(_overlap(m['start_ms'], m['end_ms'], a, b) for m in music)
        has_fx = any(_overlap(e['start_ms'], e['end_ms'], a, b) for e in sound_effects)
        layers = int(has_sp) + int(has_mu) + int(has_fx)
        if layers >= 2:
            kind = 'mixed'
            desc = '对白、配乐与环境音效叠加区间。'
        elif has_sp:
            kind = 'speech'
            desc = '以人声对白为主的音轨区间。'
        elif has_mu:
            kind = 'music'
            desc = '以背景音乐为主的音轨区间。'
        elif has_fx:
            kind = 'sound_effect'
            desc = '以环境或动作音效为主的音轨区间。'
        else:
            kind = 'silence'
            desc = default_desc or '环境底噪与静默过渡区间。'
        if intervals and intervals[-1]['kind'] == kind:
            comb_start = intervals[-1]['start_ms']
            c_sp = any(_overlap(s['start_ms'], s['end_ms'], comb_start, b) for s in speech)
            c_mu = any(_overlap(m['start_ms'], m['end_ms'], comb_start, b) for m in music)
            c_fx = any(_overlap(e['start_ms'], e['end_ms'], comb_start, b) for e in sound_effects)
            c_layers = int(c_sp) + int(c_mu) + int(c_fx)
            c_kind = 'mixed' if c_layers >= 2 else ('speech' if c_sp else ('music' if c_mu else ('sound_effect' if c_fx else 'silence')))
            if c_kind == kind:
                intervals[-1]['end_ms'] = b
                continue
        intervals.append({
            'start_ms': a,
            'end_ms': b,
            'kind': kind,
            'description': desc,
        })

    uncertainties = []
    for u in (doc.get('uncertainties') if isinstance(doc.get('uncertainties'), list) else []):
        txt = _clean_str(u, '', 240)
        if txt:
            uncertainties.append(txt)
        if len(uncertainties) >= 1:
            break

    return {
        'start_ms': core_start,
        'end_ms': core_end,
        'intervals': intervals,
        'speech': speech,
        'speakers': speakers,
        'music': music,
        'sound_effects': sound_effects,
        'uncertainties': uncertainties,
    }


def _normalize_association(value, doc):
    src = json.loads(value['source_text'])
    bundle_hash = src['bundle_sha256']
    nodes = {n['key']: n for n in src.get('nodes', []) if isinstance(n, dict) and 'key' in n}
    entity_nodes = [n for n in src.get('nodes', []) if isinstance(n, dict) and n.get('kind') == 'entities']
    speaker_nodes = [n for n in src.get('nodes', []) if isinstance(n, dict) and n.get('kind') == 'speakers']
    shot_nodes = [n for n in src.get('nodes', []) if isinstance(n, dict) and n.get('kind') == 'shots']
    src_boundaries = src.get('boundaries', [])

    seen_group_keys = set()
    assigned_entities = set()
    entities = []

    max_by_kind = {'character': 4, 'scene': 3, 'prop': 5}
    count_by_kind = {'character': 0, 'scene': 0, 'prop': 0}

    raw_entities = doc.get('entities') if isinstance(doc.get('entities'), list) else []
    for g in raw_entities:
        if not isinstance(g, dict):
            continue
        members = []
        target_kind = None
        for mk in (g.get('member_keys') if isinstance(g.get('member_keys'), list) else []):
            n = nodes.get(mk)
            if not n or n.get('kind') != 'entities' or mk in assigned_entities:
                continue
            v = n.get('value') if isinstance(n.get('value'), dict) else {}
            k = v.get('kind')
            if k not in {'character', 'scene', 'prop'}:
                continue
            if target_kind is None:
                target_kind = k
            if k == target_kind:
                members.append(mk)
                assigned_entities.add(mk)
        if not members or count_by_kind[target_kind] >= max_by_kind[target_kind]:
            for mk in members:
                assigned_entities.discard(mk)
            continue
        count_by_kind[target_kind] += 1
        first_val = nodes[members[0]].get('value') if isinstance(nodes[members[0]].get('value'), dict) else {}
        key = _clean_key(g.get('key'), f'{target_kind}_{len(entities) + 1}', seen_group_keys)
        ev = list(dict.fromkeys(members + [
            ek for ek in (g.get('evidence_keys') if isinstance(g.get('evidence_keys'), list) else [])
            if ek in nodes
        ]))
        entities.append({
            'key': key,
            'kind': target_kind,
            'name': _clean_str(g.get('name'), _clean_str(first_val.get('name'), f'{target_kind}_{len(entities) + 1}', 256), 256),
            'description': _clean_str(g.get('description'), _clean_str(first_val.get('description'), '跨分段视觉实体观察汇总。', 500), 500),
            'member_keys': members,
            'state': 'single' if len(members) == 1 else 'inferred',
            'evidence_keys': ev,
            'reason': _clean_str(g.get('reason'), '单一分段直接观察记录。' if len(members) == 1 else '根据跨分段外观、服饰与场景特征推断关联。', 300),
        })

    # Assign any remaining entity nodes into bounded buckets by kind
    for n in entity_nodes:
        mk = n['key']
        if mk in assigned_entities:
            continue
        v = n.get('value') if isinstance(n.get('value'), dict) else {}
        k = v.get('kind') if v.get('kind') in {'character', 'scene', 'prop'} else 'prop'
        if count_by_kind[k] < max_by_kind[k]:
            count_by_kind[k] += 1
            key = _clean_key(f'{k}_{len(entities) + 1}', f'{k}_{len(entities) + 1}', seen_group_keys)
            entities.append({
                'key': key,
                'kind': k,
                'name': _clean_str(v.get('name'), f'{k}_{len(entities) + 1}', 256),
                'description': _clean_str(v.get('description'), '视觉观察对象记录。', 500),
                'member_keys': [mk],
                'state': 'single',
                'evidence_keys': [mk],
                'reason': '单一分段直接观察记录。',
            })
        else:
            target_group = next(g for g in entities if g['kind'] == k)
            target_group['member_keys'].append(mk)
            if mk not in target_group['evidence_keys']:
                target_group['evidence_keys'].append(mk)
            target_group['state'] = 'inferred'
            target_group['reason'] = '根据同类视觉对象特征跨分段归并推断。'

    # Voices (speaker_nodes), bounded to <= 3 groups
    assigned_speakers = set()
    voices = []
    raw_voices = doc.get('voices') if isinstance(doc.get('voices'), list) else []
    for g in raw_voices:
        if not isinstance(g, dict) or len(voices) >= 3:
            continue
        members = [
            mk for mk in (g.get('member_keys') if isinstance(g.get('member_keys'), list) else [])
            if mk in nodes and nodes[mk].get('kind') == 'speakers' and mk not in assigned_speakers
        ]
        if not members:
            continue
        for mk in members:
            assigned_speakers.add(mk)
        first_val = nodes[members[0]].get('value') if isinstance(nodes[members[0]].get('value'), dict) else {}
        key = _clean_key(g.get('key'), f'voice_{len(voices) + 1}', seen_group_keys)
        ev = list(dict.fromkeys(members + [
            ek for ek in (g.get('evidence_keys') if isinstance(g.get('evidence_keys'), list) else [])
            if ek in nodes
        ]))
        voices.append({
            'key': key,
            'member_keys': members,
            'description': _clean_str(g.get('description'), _clean_str(first_val.get('description'), '音轨说话人声线观察。', 400), 400),
            'state': 'single' if len(members) == 1 else 'inferred',
            'evidence_keys': ev,
            'reason': _clean_str(g.get('reason'), '单段音轨声线记录。' if len(members) == 1 else '根据跨段音色与语速特征推断为同一声线组。', 300),
        })

    remaining_speakers = [n['key'] for n in speaker_nodes if n['key'] not in assigned_speakers]
    if remaining_speakers:
        if voices and len(voices) >= 3:
            voices[-1]['member_keys'].extend(remaining_speakers)
            for mk in remaining_speakers:
                if mk not in voices[-1]['evidence_keys']:
                    voices[-1]['evidence_keys'].append(mk)
            voices[-1]['state'] = 'inferred'
            voices[-1]['reason'] = '跨段人声声线归并推断。'
        else:
            first_val = nodes[remaining_speakers[0]].get('value') if isinstance(nodes[remaining_speakers[0]].get('value'), dict) else {}
            key = _clean_key(f'voice_{len(voices) + 1}', f'voice_{len(voices) + 1}', seen_group_keys)
            voices.append({
                'key': key,
                'member_keys': remaining_speakers,
                'description': _clean_str(first_val.get('description'), '音轨说话人声线集合。', 400),
                'state': 'single' if len(remaining_speakers) == 1 else 'inferred',
                'evidence_keys': list(remaining_speakers),
                'reason': '单段声线直接记录。' if len(remaining_speakers) == 1 else '跨段人声声线归并推断。',
            })

    # Shots and boundaries
    boundary_map = {(b['left_shot'], b['right_shot']): b for b in src_boundaries if isinstance(b, dict)}
    raw_decisions = {}
    for d in (doc.get('boundaries') if isinstance(doc.get('boundaries'), list) else []):
        if isinstance(d, dict) and (d.get('left_shot'), d.get('right_shot')) in boundary_map:
            raw_decisions[(d['left_shot'], d['right_shot'])] = d

    shot_groups = []
    shot_owners = {}
    for sn in shot_nodes:
        sk = sn['key']
        scope = sn.get('scope') or {'start_ms': 0, 'end_ms': 1000}
        if shot_groups:
            prev_sk = shot_groups[-1]['member_keys'][-1]
            b_info = boundary_map.get((prev_sk, sk))
            prev_end = shot_groups[-1]['end_ms']
            if b_info and b_info.get('left_continues') and b_info.get('right_continues') and prev_end == scope['start_ms']:
                shot_groups[-1]['member_keys'].append(sk)
                shot_groups[-1]['end_ms'] = scope['end_ms']
                shot_groups[-1]['state'] = 'inferred'
                shot_groups[-1]['evidence_keys'].append(sk)
                shot_groups[-1]['reason'] = '相邻分段边界两侧镜头连续，推断合并为同一连续镜头组。'
                shot_owners[sk] = shot_groups[-1]['key']
                continue
        g_key = _clean_key(f'shot_group_{len(shot_groups) + 1}', f'shot_group_{len(shot_groups) + 1}', seen_group_keys)
        shot_owners[sk] = g_key
        shot_groups.append({
            'key': g_key,
            'member_keys': [sk],
            'start_ms': scope['start_ms'],
            'end_ms': scope['end_ms'],
            'state': 'single',
            'evidence_keys': [sk],
            'reason': '保持原片时间顺序的独立镜头观察片段。',
        })

    boundaries = []
    for b_info in src_boundaries:
        left, right = b_info['left_shot'], b_info['right_shot']
        same = shot_owners.get(left) == shot_owners.get(right)
        raw_d = raw_decisions.get((left, right), {})
        decision = 'merge' if same else 'separate'
        boundaries.append({
            'left_shot': left,
            'right_shot': right,
            'decision': decision,
            'evidence_keys': [left, right],
            'reason': _clean_str(raw_d.get('reason'), '相邻分段镜头连续合并。' if same else '分段边界处保持独立镜头切分。', 240),
        })

    char_entities = {e['key']: e for e in entities if e['kind'] == 'character'}
    voice_map = {v['key']: v for v in voices}
    voice_links = []
    seen_pairs = set()
    for l in (doc.get('voice_links') if isinstance(doc.get('voice_links'), list) else []):
        if not isinstance(l, dict):
            continue
        vk, ek = l.get('voice_key'), l.get('entity_key')
        if vk not in voice_map or ek not in char_entities or (vk, ek) in seen_pairs:
            continue
        seen_pairs.add((vk, ek))
        v_mem = voice_map[vk]['member_keys']
        e_mem = char_entities[ek]['member_keys']
        ev = list(dict.fromkeys([v_mem[0], e_mem[0]]))
        voice_links.append({
            'voice_key': vk,
            'entity_key': ek,
            'state': l.get('state') if l.get('state') in {'inferred', 'undetermined'} else 'inferred',
            'evidence_keys': ev,
            'reason': _clean_str(l.get('reason'), '基于画面人物出场与同期人声特征的候选关联（保留不确定性）。', 240),
        })
        if len(voice_links) >= 2:
            break
    if not voice_links and voices and char_entities:
        vk = voices[0]['key']
        ek = next(iter(char_entities))
        voice_links.append({
            'voice_key': vk,
            'entity_key': ek,
            'state': 'inferred',
            'evidence_keys': list(dict.fromkeys([voices[0]['member_keys'][0], char_entities[ek]['member_keys'][0]])),
            'reason': '基于画面主要角色与主要人声声线的候选关联（未作声纹验证）。',
        })

    uncertainties = []
    for u in (doc.get('uncertainties') if isinstance(doc.get('uncertainties'), list) else []):
        txt = _clean_str(u, '', 240)
        if txt:
            uncertainties.append(txt)
        if len(uncertainties) >= 1:
            break
    if not uncertainties:
        uncertainties.append('跨分段人物、道具与声线归并均保留为推断候选，不代表已验证演员或声纹身份。')

    return {
        'schema_version': 1,
        'bundle_sha256': bundle_hash,
        'entities': entities,
        'shot_groups': shot_groups,
        'voices': voices,
        'voice_links': voice_links,
        'boundaries': boundaries,
        'uncertainties': uncertainties,
    }


def _sanitize_placeholders(text):
    tokens = []
    def _save(match):
        tokens.append(match.group(1))
        return f'__VAR_TOKEN_{len(tokens) - 1}__'
    cleaned = PLACEHOLDER_RE.sub(_save, text)
    cleaned = cleaned.replace('{{', '【').replace('}}', '】')
    for idx, name in enumerate(tokens):
        cleaned = cleaned.replace(f'__VAR_TOKEN_{idx}__', f'{{{{{name}}}}}')
    return cleaned, list(dict.fromkeys(tokens))


def _normalize_prompts(value, doc):
    src = json.loads(value['source_text'])
    batch = src['batch']
    batch_key = batch['batch_key']
    scopes = {s['key']: s.get('evidence_keys', []) for s in batch.get('scopes', []) if isinstance(s, dict)}
    targets = {t['key']: t for t in batch.get('targets', []) if isinstance(t, dict)}
    pairs = batch.get('pairs', [])

    raw_items = doc.get('items') if isinstance(doc.get('items'), list) else []
    by_pair = {}
    for item in raw_items:
        if isinstance(item, dict):
            by_pair[(item.get('scope_key'), item.get('target_key'))] = item

    items = []
    for idx, p in enumerate(pairs):
        s_key, t_key = p['scope_key'], p['target_key']
        target = targets.get(t_key, {})
        raw = by_pair.get((s_key, t_key))
        if raw is None and idx < len(raw_items) and isinstance(raw_items[idx], dict):
            raw = raw_items[idx]
        raw = raw or {}

        label = target.get('requested_label', t_key)
        task_type = target.get('task_type', 'generation')
        input_mode = target.get('input_mode', 'text')
        title = _clean_str(raw.get('title'), f'{s_key} · {label} 重建提示词候选', 256)
        intent = _clean_str(raw.get('creative_intent'), f'基于原片 {s_key} 观察证据，面向 {label} ({task_type}/{input_mode}) 整理可复用提示词结构。', 1200)
        prompt_body = _clean_str(
            raw.get('prompt_text'),
            '主体与场景设定：{{subject_description}}；构图与机位节奏：{{camera_and_lighting}}；保持原片叙事张力与动作先后关系。',
            8000,
        )
        prompt_body, used_vars = _sanitize_placeholders(prompt_body)
        if not used_vars:
            prompt_body = prompt_body + '（可替换参数：{{subject_description}}）'
            used_vars = ['subject_description']

        raw_var_map = {
            v.get('key'): v for v in (raw.get('variables') if isinstance(raw.get('variables'), list) else [])
            if isinstance(v, dict) and isinstance(v.get('key'), str)
        }
        variables = []
        for vk in used_vars:
            rv = raw_var_map.get(vk, {})
            variables.append({
                'key': vk,
                'description': _clean_str(rv.get('description'), f'用于替换 {vk} 的具体创作输入描述。', 400),
                'required': bool(rv.get('required', True)),
            })

        adaptation = _clean_str(
            raw.get('adaptation_note'),
            f'按目标 {label} ({task_type} / {input_mode}) 适配；未声称恢复原作者提示词，亦未执行目标模型调用。',
            1200,
        )
        missing = [_clean_str(m, '', 240) for m in (raw.get('missing_inputs') if isinstance(raw.get('missing_inputs'), list) else [])]
        missing = [m for m in missing if m][:1] or ['新创作目标的具体替换参数与参考素材']
        uncert = [_clean_str(u, '', 240) for u in (raw.get('uncertainties') if isinstance(raw.get('uncertainties'), list) else [])]
        uncert = [u for u in uncert if u][:1] or ['由观察证据逆向归纳的提示词候选，目标模型生成效果需另行运行验证。']

        item_out = {
            'scope_key': s_key,
            'target_key': t_key,
            'title': title,
            'creative_intent': intent,
            'prompt_text': prompt_body,
            'adaptation_note': adaptation,
            'variables': variables,
            'evidence_keys': list(dict.fromkeys(scopes.get(s_key, []))),
            'missing_inputs': missing,
            'uncertainties': uncert,
        }
        neg = _clean_str(raw.get('negative_prompt'), '', 1200)
        if neg:
            neg_clean, neg_vars = _sanitize_placeholders(neg)
            if not (set(neg_vars) - set(used_vars)) and neg_clean:
                item_out['negative_prompt'] = neg_clean
        items.append(item_out)

    return {
        'schema_version': 1,
        'batch_key': batch_key,
        'items': items,
    }


def _time_locator(start_ms, end_ms):
    s = max(0, int(start_ms))
    e = max(s + 1, int(end_ms))
    return {'kind': 'time', 'start_tick': s, 'end_tick': e, 'timebase_num': 1, 'timebase_den': 1000}


def _normalize_result_payload(kind, raw_payload, scope, duration_ms):
    p = raw_payload if isinstance(raw_payload, dict) else {}
    rng = scope.get('range') if isinstance(scope, dict) else None
    w_start = int(rng['start_ms']) if isinstance(rng, dict) and 'start_ms' in rng else 0
    w_end = int(rng['end_ms']) if isinstance(rng, dict) and 'end_ms' in rng else max(1000, int(duration_ms or 1000))
    if w_end <= w_start:
        w_end = w_start + 1000
    exact_scope = _time_locator(w_start, w_end)

    if kind == 'character':
        ck = p.get('character_kind') if p.get('character_kind') in {'fictional', 'research_subject', 'performer_reference'} else 'fictional'
        out = {
            'character_kind': ck,
            'identity_notes': _clean_str(p.get('identity_notes'), '基于原片视觉观察归纳的角色身份与出场特征说明。', 800),
            'appearance': _clean_str(p.get('appearance'), '原片镜头内可见的面部、体型与外貌特征。', 800),
            'costume': _clean_str(p.get('costume'), '原片镜头内可见的服装款式、色彩与配饰特征。', 800),
            'performance_notes': _clean_str(p.get('performance_notes'), '镜头内观察到的神态、肢体动作与表演方式。', 800),
        }
        for field in ('motives', 'traits'):
            vals = [_clean_str(x, '', 160) for x in (p.get(field) if isinstance(p.get(field), list) else [])]
            vals = [x for x in vals if x][:4]
            if vals:
                out[field] = vals
        return out

    if kind == 'scene':
        return {
            'setting': _clean_str(p.get('setting'), '原片镜头呈现的室内外环境设定与时代氛围。', 800),
            'spatial_layout': _clean_str(p.get('spatial_layout'), '前中后景空间层次、通道关系与机位活动区域。', 800),
            'prop_roles': _clean_str(p.get('prop_roles'), '场景内陈设与交互道具的空间分布与叙事作用。', 800),
            'material_notes': _clean_str(p.get('material_notes'), '地面、墙面及主要陈设的表面材质与反光特征。', 800),
            'lighting_notes': _clean_str(p.get('lighting_notes'), '主光方向、色温冷暖对比与阴影层次。', 800),
        }

    if kind == 'prop':
        pts = []
        seen_pts = set()
        for idx, pt in enumerate((p.get('interaction_points') if isinstance(p.get('interaction_points'), list) else [])[:3]):
            if not isinstance(pt, dict):
                continue
            k = _clean_key(pt.get('key'), f'point_{idx + 1}', seen_pts)
            pts.append({'key': k, 'purpose': _clean_str(pt.get('purpose'), '角色持握或触发动作的交互部位。', 400)})
        return {
            'description': _clean_str(p.get('description'), '原片画面中可见的道具外观、形态与细节特征。', 800),
            'function': _clean_str(p.get('function'), '在叙事与角色动作中承担的具体功能。', 800),
            'state': _clean_str(p.get('state'), '镜头内呈现的物理状态与变化过程。', 800),
            'scale_basis': _clean_str(p.get('scale_basis'), '以人物手部及周边常见陈设为参照推断的相对尺度。', 400),
            'interaction_points': pts,
        }

    if kind == 'shot':
        beats = []
        for b in (p.get('action_beats') if isinstance(p.get('action_beats'), list) else [])[:2]:
            if isinstance(b, dict):
                beats.append({
                    'scope': exact_scope,
                    'action': _clean_str(b.get('action'), '镜头内主体与机位协同推进的关键动作节拍。', 500),
                })
        if not beats:
            beats = [{'scope': exact_scope, 'action': '镜头核心范围内的连续画面动作节拍。'}]
        return {
            'segment_kind': 'edit_shot',
            'scope': exact_scope,
            'shot_scale': _clean_str(p.get('shot_scale'), '中景至近景构图', 120),
            'camera_angle': _clean_str(p.get('camera_angle'), '平视机位角度', 200),
            'camera_motion': _clean_str(p.get('camera_motion'), '机位保持平稳构图或轻微跟随运动。', 500),
            'subject_motion': _clean_str(p.get('subject_motion'), '主体在画面核心区域完成动作与视线交互。', 500),
            'graphic_motion': _clean_str(p.get('graphic_motion'), '画面字幕或图形元素按时间范围呈现。', 350),
            'composition': _clean_str(p.get('composition'), '主体突出、前后景层次清晰的电影感构图。', 500),
            'lens_note': _clean_str(p.get('lens_note'), '标准焦段自然透视与景深控制。', 350),
            'transition': _clean_str(p.get('transition'), '硬切衔接相邻镜头。', 240),
            'action_beats': beats,
        }

    if kind == 'sound_cue':
        s_key = scope.get('key', '') if isinstance(scope, dict) else ''
        if s_key.startswith('voice.'):
            sk = 'voice_reference'
        elif s_key.startswith('speech.'):
            sk = p.get('sound_kind') if p.get('sound_kind') in {'dialogue', 'narration'} else 'dialogue'
        elif s_key.startswith('sound_effects.'):
            sk = p.get('sound_kind') if p.get('sound_kind') in {'sfx', 'ambience'} else 'sfx'
        else:
            sk = p.get('sound_kind') if p.get('sound_kind') in {'dialogue', 'narration', 'sfx', 'ambience', 'voice_reference'} else 'sfx'
        out = {
            'sound_kind': sk,
            'description': _clean_str(p.get('description'), '原片音轨中听取到的声音特征与情绪层次描述。', 600),
            'scope': exact_scope,
        }
        tr = _clean_str(p.get('transcript'), '', 600)
        if tr:
            out['transcript'] = tr
        return out

    if kind == 'music_structure':
        layers = []
        for l in (p.get('instrument_layers') if isinstance(p.get('instrument_layers'), list) else [])[:2]:
            if isinstance(l, dict):
                layers.append({
                    'instrument': _clean_str(l.get('instrument'), '主奏合成器与弦乐铺底', 200),
                    'role': _clean_str(l.get('role'), '烘托叙事氛围与推进节奏层次。', 350),
                    'scope': exact_scope,
                })
        if not layers:
            layers = [{'instrument': '背景伴奏配器层', 'role': '承托本段画面情绪与节奏。', 'scope': exact_scope}]
        return {
            'section_kind': _clean_str(p.get('section_kind'), 'narrative_underscore', 120),
            'scope': exact_scope,
            'instrument_layers': layers,
        }

    if kind == 'script_unit':
        uk = p.get('unit_kind') if p.get('unit_kind') in {'synopsis', 'episode', 'scene', 'beat', 'character', 'event', 'object', 'dialogue'} else 'synopsis'
        return {
            'unit_kind': uk,
            'text': _clean_str(p.get('text'), '基于全片观察证据整理的叙事主线、人物互动与冲突推进梗概。', 3000),
            'premise': _clean_str(p.get('premise'), '角色在特定情境下围绕核心道具与目标展开的戏剧冲突。', 600),
            'theme': _clean_str(p.get('theme'), '通过人物抉择与情节反转呈现的叙事主题。', 600),
            'goal': _clean_str(p.get('goal'), '画面与对白证据所呈现的角色阶段性行动目标。', 600),
            'conflict': _clean_str(p.get('conflict'), '角色之间及环境阻碍所构成的正面戏剧冲突。', 600),
            'stakes': _clean_str(p.get('stakes'), '行动成败对人物处境与关系的直接影响。', 600),
            'reversal': _clean_str(p.get('reversal'), '关键道具或动作触发的局势转折。', 600),
            'hook': _clean_str(p.get('hook'), '开篇建立悬念与吸引观众注意力的核心钩子。', 600),
            'story_time': _clean_str(p.get('story_time'), f'全片连续叙事时段（0ms 至 {w_end}ms）。', 300),
        }

    if kind == 'style':
        seen_rules = set()
        rules = []
        for idx, r in enumerate((p.get('rules') if isinstance(p.get('rules'), list) else [])[:4]):
            if not isinstance(r, dict):
                continue
            rk = _clean_key(r.get('key'), f'style_rule_{idx + 1}', seen_rules)
            dom = r.get('domain') if r.get('domain') in STYLE_DOMAINS else 'shot'
            rules.append({
                'key': rk,
                'domain': dom,
                'instruction': _clean_str(r.get('instruction'), '保持景别切换与动作节拍的紧凑呼应。', 600),
                'evidence_refs': [],
            })
        if not rules:
            rules = [{
                'key': 'style_rule_1',
                'domain': 'shot',
                'instruction': '以近景情绪反应与关键道具特写交替推进叙事张力。',
                'evidence_refs': [],
            }]
        pos = [_clean_str(x, '', 400) for x in (p.get('positive_examples') if isinstance(p.get('positive_examples'), list) else [])]
        pos = [x for x in pos if x][:2] or ['在关键动作触发点切入特写镜头强化视觉重心。']
        neg = [_clean_str(x, '', 400) for x in (p.get('counterexamples') if isinstance(p.get('counterexamples'), list) else [])]
        neg = [x for x in neg if x][:2] or ['避免脱离角色动作逻辑的无意义机位晃动。']
        pal = [x for x in (p.get('palette') if isinstance(p.get('palette'), list) else []) if isinstance(x, str) and HEX_COLOR_RE.match(x)][:5]
        out = {
            'scope_note': _clean_str(p.get('scope_note'), '仅代表本参考视频观察到的视听风格特征，不代表特定创作者完整体系。', 600),
            'rules': rules,
            'positive_examples': pos,
            'counterexamples': neg,
        }
        if pal:
            out['palette'] = pal
        return out

    if kind == 'method':
        inputs = [
            {'key': 'source_premise', 'value_type': 'string', 'required': True, 'description': '新创作场次的人物关系与冲突设定。'},
            {'key': 'key_prop_spec', 'value_type': 'string', 'required': True, 'description': '承担动作转折的核心道具描述。'},
        ]
        steps = [
            {
                'key': 'step_blocking_plan',
                'instruction': _clean_str(
                    (p.get('steps')[0].get('instruction') if isinstance(p.get('steps'), list) and p.get('steps') and isinstance(p.get('steps')[0], dict) else ''),
                    '依据冲突设定与核心道具规划空间调度与分镜节拍序列。',
                    600,
                ),
                'input_keys': ['source_premise', 'key_prop_spec'],
                'output_keys': ['shot_sequence_plan'],
            },
            {
                'key': 'step_av_synthesis',
                'instruction': '结合分镜节拍配置同期人声对白、环境音效与情绪铺底音乐。',
                'input_keys': ['shot_sequence_plan'],
                'output_keys': ['reusable_scene_package'],
            },
        ]
        constraints = [{
            'key': 'constraint_evidence_boundary',
            'condition': '当关键动作涉及道具交互或跨镜头衔接时',
            'effect': '必须保持道具空间位置、人物视线方向与声音提示的一致性。',
        }]
        expected_outputs = [
            {
                'key': 'shot_sequence_plan',
                'type_key': 'shot',
                'description': '按节拍拆解的分镜与机位运动方案。',
                'checks': ['每个节拍具备明确起止时码与主体动作说明。'],
            },
            {
                'key': 'reusable_scene_package',
                'type_key': 'creative_system',
                'description': '包含角色、场景、道具与视听节奏的可复用创作结构包。',
                'checks': ['所有推断与缺失输入均已显式标注。'],
            },
        ]
        counter = [_clean_str(x, '', 400) for x in (p.get('counterexamples') if isinstance(p.get('counterexamples'), list) else [])]
        counter = [x for x in counter if x][:2] or ['禁止仅凭风格标签跳过具体动作节拍与道具交互步骤。']
        purpose = p.get('purpose') if p.get('purpose') in {'creative_reuse', 'analysis_procedure', 'production_procedure'} else 'creative_reuse'
        return {
            'purpose': purpose,
            'objective': _clean_str(p.get('objective'), '从原片视听拆解证据提炼可迁移的短剧分镜与道具叙事创作方法。', 600),
            'applicability': _clean_str(p.get('applicability'), '适用于双人或多人戏剧冲突、道具触发反转的叙事片段创作。', 600),
            'inputs': inputs,
            'steps': steps,
            'constraints': constraints,
            'expected_outputs': expected_outputs,
            'counterexamples': counter,
        }

    return p


def _normalize_results(value, doc):
    src = json.loads(value['source_text'])
    batch = src['batch']
    batch_key = batch['batch_key']
    type_key = batch['type_key']
    scopes = batch.get('scopes', [])
    obs = src.get('observations', {})
    duration_ms = (obs.get('source') or {}).get('duration_ms', 345003)
    valid_nodes = [
        n['key'] for n in (obs.get('nodes') or [])
        if isinstance(n, dict) and isinstance(n.get('key'), str) and n.get('kind') != 'uncertainties'
    ]
    valid_node_set = set(valid_nodes)

    raw_items = doc.get('items') if isinstance(doc.get('items'), list) else []
    by_scope = {}
    for item in raw_items:
        if isinstance(item, dict) and isinstance(item.get('scope_key'), str):
            by_scope[item['scope_key']] = item

    items = []
    for idx, scope in enumerate(scopes):
        s_key = scope['key']
        raw = by_scope.get(s_key)
        if raw is None and idx < len(raw_items) and isinstance(raw_items[idx], dict):
            raw = raw_items[idx]
        raw = raw or {}

        req_ev = [k for k in (scope.get('evidence_keys') or []) if k in valid_node_set]
        extra_ev = [
            k for k in (raw.get('evidence_keys') if isinstance(raw.get('evidence_keys'), list) else [])
            if k in valid_node_set and k not in req_ev
        ][:2]
        ev = list(dict.fromkeys(req_ev + extra_ev))
        if not ev and valid_nodes:
            ev = valid_nodes[:4]

        raw_payloads = raw.get('payloads') if isinstance(raw.get('payloads'), list) and raw.get('payloads') else [{}]
        norm_payload = _normalize_result_payload(type_key, raw_payloads[0], scope, duration_ms)

        basis = raw.get('basis') if raw.get('basis') in {'observed', 'inferred', 'reconstruction'} else ('inferred' if s_key == 'whole' else 'observed')
        missing = [_clean_str(m, '', 240) for m in (raw.get('missing_inputs') if isinstance(raw.get('missing_inputs'), list) else [])]
        missing = [m for m in missing if m][:1]
        uncert = [_clean_str(u, '', 240) for u in (raw.get('uncertainties') if isinstance(raw.get('uncertainties'), list) else [])]
        uncert = [u for u in uncert if u][:1] or ['由固定观察证据归纳的候选成果，未代表人工审核或工程文件交付。']

        items.append({
            'scope_key': s_key,
            'title': _clean_str(raw.get('title'), f'{type_key} · {s_key} 拆解成果候选', 256),
            'state': 'ready',
            'basis': basis,
            'basis_note': _clean_str(raw.get('basis_note'), f'基于原片观察节点（{len(ev)} 项证据）整理的 {type_key} 结构化成果。', 600),
            'evidence_keys': ev,
            'missing_inputs': missing,
            'uncertainties': uncert,
            'payloads': [norm_payload],
        })

    return {
        'schema_version': 1,
        'batch_key': batch_key,
        'type_key': type_key,
        'items': items,
    }


def normalize_output(operation, value, parsed):
    """Canonicalize a parsed Gemini JSON dict for World analysis stages."""
    stage = detect_stage(operation, value)
    if stage is None:
        return parsed
    doc = parsed if isinstance(parsed, dict) else {}
    try:
        if stage == 'visual':
            return _normalize_visual(value, doc)
        if stage == 'audio':
            return _normalize_audio(value, doc)
        if stage == 'association':
            return _normalize_association(value, doc)
        if stage == 'prompts':
            return _normalize_prompts(value, doc)
        if stage == 'results':
            return _normalize_results(value, doc)
    except Exception:
        return parsed
    return parsed
