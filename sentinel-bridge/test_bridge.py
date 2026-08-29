import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

import bridge


def test_build_delegation_prompt_includes_token_and_task():
    task_id = "11111111-2222-3333-4444-555555555555"
    prompt = bridge.build_delegation_prompt("检查磁盘", task_id)

    assert task_id.replace("-", "") in prompt
    assert "检查磁盘" in prompt
    assert "SENTINEL_DONE_" in prompt


def test_extract_task_response_finds_marker_after_assistant_marker():
    task_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    token = task_id.replace("-", "")
    raw = f"some earlier noise\n● 这是最终总结\nSENTINEL_DONE_{token}"

    result = bridge.extract_task_response(raw, task_id)

    assert result == "这是最终总结"


def test_extract_task_response_returns_none_when_marker_missing():
    task_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    raw = "Claude finished but forgot to print anything useful"

    result = bridge.extract_task_response(raw, task_id)

    assert result is None


def test_extract_task_response_falls_back_without_assistant_marker():
    task_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    token = task_id.replace("-", "")
    raw = f"raw terminal text with no bullet marker\nSENTINEL_DONE_{token}"

    result = bridge.extract_task_response(raw, task_id)

    assert result == "raw terminal text with no bullet marker"
