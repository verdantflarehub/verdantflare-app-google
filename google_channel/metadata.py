"""Read analysis declarations from the installed Chart; adapter limits stay in code."""
from pathlib import Path

import yaml

from .analysis import MODELS


def analysis_capabilities(path):
    if not path:
        return []
    raw = Path(path).read_bytes()
    if len(raw) > 262144:
        raise ValueError('invalid_channel_metadata')
    document = yaml.safe_load(raw)
    if (not isinstance(document, dict) or document.get('schema_version') != 1
            or document.get('channel_id') != 'google' or not isinstance(document.get('capabilities'), list)):
        raise ValueError('invalid_channel_metadata')
    result, seen = [], set()
    for item in document['capabilities']:
        if not isinstance(item, dict):
            raise ValueError('invalid_channel_metadata')
        if item.get('capability_id') != 'analysis.analyze':
            continue  # Existing image/video registration is not migrated here.
        model = item.get('model_id')
        if (model not in MODELS or model in seen or item.get('type') != 'analysis'
                or item.get('operation') != 'analysis.analyze'
                or item.get('execution_state') not in {'enabled', 'disabled', 'adapter_connected_unverified'}
                or not isinstance(item.get('parameter_schema'), dict)):
            raise ValueError('invalid_analysis_metadata')
        seen.add(model)
        result.append(item)
    return result
