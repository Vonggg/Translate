import json
from pathlib import Path
import pytest
from support.project_context import materialize_context


def test_defaults_identity_and_future_fields(tmp_path):
    project = tmp_path / 'Game'
    project.mkdir()
    base = tmp_path / 'base.json'
    base.write_text(json.dumps({'project_name':'Other', 'future_option':{'x':7}, 'enable_ai_field_review':True}))
    context = tmp_path / 'context.json'
    context.write_text(json.dumps({'schema_version':1,'project_name':'Game','project_root_dir':str(tmp_path)}))
    output = tmp_path / 'session.json'
    before = base.read_bytes()
    materialize_context(base, context, output)
    result = json.loads(output.read_text())
    assert result['project_name'] == 'Game'
    assert result['future_option'] == {'x':7}
    assert base.read_bytes() == before
    base.write_text(json.dumps({'future_option':{'x':8}}))
    materialize_context(base, context, output)
    assert json.loads(output.read_text())['future_option'] == {'x':8}
    data = json.loads(context.read_text())
    data['enable_ai_field_review'] = False
    context.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        materialize_context(base, context, output)


def test_launcher_context_inherits_to_child_and_snapshot_is_removed(tmp_path, monkeypatch):
    import argparse
    import os
    import sys
    from types import SimpleNamespace
    import run_with_config_python as launcher
    from one_click_pipeline import _default_run_state_path
    project = tmp_path / 'Game'
    project.mkdir()
    context = project / '.translate/project-context.json'
    context.parent.mkdir()
    context.write_text(json.dumps({'schema_version':1,'project_name':'Game','project_root_dir':str(tmp_path)}))
    base = tmp_path / 'base.json'
    base.write_text(json.dumps({'project_name':'Other','python_executable':sys.executable,'future_setting':42}))
    monkeypatch.setattr(launcher, 'DEFAULT_CONFIG_PATH', base)
    monkeypatch.setattr(launcher, 'parse_args', lambda: argparse.Namespace(project_context=context,config=None,channel_package_dir_name='GAME_hongtu_L',print_python=False,script='main.py',script_args=[]))
    snapshots = []
    def child(*args, **kwargs):
        env = kwargs['env']
        path = Path(env['TRANSLATE_CONFIG_PATH'])
        snapshots.append(path)
        cfg = json.loads(path.read_text())
        assert cfg['future_setting'] == 42 and cfg['project_name'] == 'Game'
        assert env['TRANSLATE_CHANNEL_PACKAGE_DIR_NAME'] == 'GAME_hongtu_L'
        assert _default_run_state_path(path).parent == context.parent
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(launcher.subprocess, 'run', child)
    assert launcher.main() == 0
    assert snapshots and not snapshots[0].exists()
    assert json.loads(base.read_text())['project_name'] == 'Other'
