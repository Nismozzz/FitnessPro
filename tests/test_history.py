from types import SimpleNamespace

from app.context.history import fit_recent_groups, repair_messages, rows_to_groups, serialize_groups
from app.llm.tokens import estimate_tokens, truncate_to_tokens


def row(seq, turn, role, content=None, tool_calls=None, tool_call_id=None):
    return SimpleNamespace(
        seq=seq, turn_id=turn, role=role, content=content, tool_calls=tool_calls, tool_call_id=tool_call_id
    )


def call(cid, name="search_exercises", args="{}"):
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": args}}


def test_estimate_and_truncate():
    assert estimate_tokens("") == 0
    assert estimate_tokens("深蹲") == 2
    assert estimate_tokens("abcd") == 1
    long = "卧推" * 500
    cut = truncate_to_tokens(long, 50)
    assert cut.startswith("卧推") and "已截断" in cut
    assert estimate_tokens(cut.split("…")[0]) <= 50


def test_repair_drops_dangling_tool_calls():
    msgs = [
        {"role": "user", "content": "推荐胸部动作"},
        {"role": "assistant", "content": None, "tool_calls": [call("a"), call("b")]},
        {"role": "tool", "tool_call_id": "a", "content": "{}"},
        {"role": "tool", "tool_call_id": "zzz", "content": "orphan"},
    ]
    repaired = repair_messages(msgs)
    assert [m["role"] for m in repaired] == ["user", "assistant", "tool"]
    assert [c["id"] for c in repaired[1]["tool_calls"]] == ["a"]


def test_repair_removes_empty_assistant_without_results():
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": None, "tool_calls": [call("x")]},
    ]
    assert [m["role"] for m in repair_messages(msgs)] == ["user"]


def test_groups_by_turn_and_fit_is_contiguous():
    rows = []
    seq = 0
    for t in range(5):
        seq += 1
        rows.append(row(seq, f"t{t}", "user", "问题" * 50))
        seq += 1
        rows.append(row(seq, f"t{t}", "assistant", "回答" * 100))
    groups = rows_to_groups(rows, 400)
    assert [g.turn_id for g in groups] == [f"t{i}" for i in range(5)]
    assert groups[1].first_seq == 3

    per_turn = groups[0].tokens
    kept, dropped = fit_recent_groups(groups, budget=per_turn * 2 + 1)
    assert [g.turn_id for g in kept] == ["t3", "t4"]
    assert dropped == 3

    # 预算再小也保留最新一轮
    kept, dropped = fit_recent_groups(groups, budget=1)
    assert [g.turn_id for g in kept] == ["t4"]


def test_history_tool_results_truncated_and_serialized():
    rows = [
        row(1, "t", "user", "练背"),
        row(2, "t", "assistant", None, tool_calls=[call("c1", args='{"query":"背"}')]),
        row(3, "t", "tool", "x" * 10000, tool_call_id="c1"),
        row(4, "t", "assistant", "推荐引体向上"),
    ]
    (group,) = rows_to_groups(rows, 100)
    assert group.messages[2]["content"].endswith("tokens]")
    text = serialize_groups([group])
    assert "search_exercises" in text and "[工具结果 search_exercises]" in text and "推荐引体向上" in text
