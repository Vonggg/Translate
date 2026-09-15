import json
import os
import subprocess
import sys
import time
import pytest
from types import SimpleNamespace
from unittest.mock import patch

from pipeline.dynamic_translation_dictionary import generate_dynamic_translation_dictionary
from support.timed_confirmation import confirm_dictionary_translation


def test_decline_leaves_raw_dictionary_and_does_not_call_builder(tmp_path):
    source = tmp_path / 'stringliteral.json'
    source.write_text(json.dumps([{'value': 'Chest', 'address': '0x100'}]))
    cfg = SimpleNamespace(stringliteral_json_path=source, stage_record_dir=tmp_path / 'records',
                          stage_dir=tmp_path / 'output')
    def confirm():
        cpp = next(cfg.stage_dir.rglob('native_unity_translation_dictionary.generated.cpp'))
        assert '{u"Chest", u""}' in cpp.read_text(encoding='utf-8')
        return False
    def builder(*args, **kwargs):
        raise AssertionError('translation was declined')
    output = generate_dynamic_translation_dictionary(cfg, translation_builder=builder,
        translation_confirmation=confirm, usage_analyzer=lambda **kwargs: {
            'exact_literals': [{'value': 'Chest', 'address': '0x100', 'evidence': []}],
            'derived_influence': [], 'unresolved': [], 'stats': {}})
    assert '{u"Chest", u""}' in output.read_text(encoding='utf-8')


def test_timeout_defaults_to_allow():
    assert confirm_dictionary_translation(timeout=0) is True


def test_windows_no_key_declines():
    with patch('support.timed_confirmation.os.name', 'nt'), \
         patch('support.timed_confirmation.sys.stdin', SimpleNamespace(isatty=lambda: True)), \
         patch.dict('sys.modules', {'msvcrt': SimpleNamespace(kbhit=lambda: True, getwch=lambda: 'n')}):
        assert confirm_dictionary_translation(timeout=30) is False


@pytest.mark.skipif(os.name != 'nt', reason='Windows pipe integration')
@pytest.mark.parametrize('answer,expected', [('n', False), ('N', False), ('y', True)])
def test_real_windows_pipe_consumes_answer_not_next_menu(answer, expected):
    code = ('from support.timed_confirmation import confirm_dictionary_translation; '
            'print("RESULT",confirm_dictionary_translation(2),flush=True); '
            'print("NEXT",input(),flush=True)')
    result = subprocess.run([sys.executable, '-u', '-c', code], input=answer + '\nmenu-next\n',
                            capture_output=True, text=True, encoding='utf-8', timeout=5)
    assert result.returncode == 0, result.stderr
    assert f'RESULT {expected}' in result.stdout
    assert 'NEXT menu-next' in result.stdout


@pytest.mark.skipif(os.name != 'nt', reason='Windows pipe integration')
def test_timeout_does_not_leave_background_pipe_reader():
    code = ('from support.timed_confirmation import confirm_dictionary_translation; '
            'print("RESULT",confirm_dictionary_translation(0.2),flush=True); '
            'print("NEXT",input(),flush=True)')
    process = subprocess.Popen([sys.executable, '-u', '-c', code], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8')
    try:
        time.sleep(0.6)
        output, error = process.communicate('menu-after-timeout\n', timeout=5)
        assert process.returncode == 0, error
        assert 'RESULT True' in output
        assert 'NEXT menu-after-timeout' in output
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
