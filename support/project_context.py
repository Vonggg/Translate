"""Build a session-only config using Translate defaults and whitelisted identity."""
import json
from pathlib import Path


def materialize_context(base_config, context_file, output):
    base_config, context_file, output = map(Path, (base_config, context_file, output))
    raw = json.loads(base_config.read_text(encoding='utf-8-sig'))
    context = json.loads(context_file.read_text(encoding='utf-8-sig'))
    allowed = {'schema_version', 'project_name', 'project_root_dir'}
    if set(context) - allowed or context.get('schema_version') != 1:
        raise ValueError('Unsupported project context fields/version')
    name = context.get('project_name')
    if not isinstance(name, str) or not name.strip() or any(c in name for c in '/\\') or name in {'.', '..'}:
        raise ValueError('Invalid project name')
    root = Path(context['project_root_dir'])
    if not root.is_absolute() or not (root / name).is_dir():
        raise ValueError('Project directory does not exist or is not absolute')
    raw.update(project_name=name, project_root_dir=str(root.resolve()))
    output.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding='utf-8')
    summary = {'project_name': name, 'base_config': str(base_config.resolve()),
               'enable_ai_field_review': bool(raw.get('enable_ai_field_review', False))}
    summary_path = root / name / '.translate/effective-config-summary.json'
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    return output
