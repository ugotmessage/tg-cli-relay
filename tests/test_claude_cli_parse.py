from tg_cli_relay.providers.claude_cli import parse_claude_output


def test_single_json_result():
    blob = '{"result": "你好", "session_id": "abc-123"}'
    text, sid = parse_claude_output(blob)
    assert text == "你好"
    assert sid == "abc-123"


def test_jsonl_subagent_then_result():
    blob = "\n".join(
        [
            '{"parent_tool_use_id":"toolu_x","session_id":"bb129a7b","subagent_type":"general-purpose","task_description":"補跑 stock-report"}',
            '{"type":"result","subtype":"success","result":"完成。6257 矽格報告已生成","session_id":"bb129a7b-b0f0-438c-a641-d041287bc765"}',
        ]
    )
    text, sid = parse_claude_output(blob)
    assert text == "完成。6257 矽格報告已生成"
    assert sid == "bb129a7b-b0f0-438c-a641-d041287bc765"


def test_jsonl_does_not_leak_raw():
    blob = '{"subagent_type":"general-purpose"}\n{"usage":{"input_tokens":3}}'
    text, sid = parse_claude_output(blob)
    assert text == ""
    assert sid is None


def test_plain_text_fallback():
    text, sid = parse_claude_output("not json at all")
    assert text == "not json at all"
    assert sid is None


def test_filters_background_process_json_leak():
    blob = (
        "[Background process proc_ea811cb98c2d finished with exit code 0~ Here's the final output:\n"
        'tool_use":{"web_search_requests":0},"modelUsage":{"claude-sonnet-4-6":{"inputTokens":19}},'
        '"terminal_reason":"completed","uuid":"68b8d663-46f2-49af-b78f-dbaecb831f78"}'
    )
    text, sid = parse_claude_output(blob)
    assert text == ""
    assert sid is None


def test_background_process_keeps_human_summary():
    blob = (
        "[Background process proc_747ada570d65 finished with exit code 0] Here's the final output:\n"
        "═══ UA Runner Summary ═══\n"
        "  Attempted: 1 labels\n"
        "  SNote uploaded: 1\n"
        "  Failed: 0\n"
        "═══════════════════════════"
    )
    text, sid = parse_claude_output(blob)
    assert "UA Runner Summary" in text
    assert "Attempted: 1 labels" in text


def test_prefers_final_result_over_internal_json():
    blob = "\n".join(
        [
            '{"subagent_type":"general-purpose","usage":{"input_tokens":3}}',
            '{"type":"result","subtype":"success","result":"任務完成","session_id":"bb129a7b"}',
        ]
    )
    text, sid = parse_claude_output(blob)
    assert text == "任務完成"
    assert sid == "bb129a7b"
