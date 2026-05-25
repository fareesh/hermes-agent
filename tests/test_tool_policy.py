from hermes_cli.plugins import get_pre_tool_call_block_message
from tools.tool_policy import evaluate_tool_policy, enforce_tool_policy


def test_disabled_policy_allows_without_prompt(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"tool_policy": {"enabled": False, "rules": [{"tools": ["read_file"], "decision": "deny"}]}},
    )

    assert enforce_tool_policy("read_file", {"path": "secret.txt"}) is None


def test_matching_deny_rule_blocks(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "tool_policy": {
                "enabled": True,
                "rules": [
                    {
                        "name": "deny_env_reads",
                        "tools": ["read_file"],
                        "when": {"path": {"regex": r"(^|/)\.env$"}},
                        "decision": "deny",
                    }
                ],
            }
        },
    )

    block = enforce_tool_policy("read_file", {"path": ".env"})
    assert block is not None
    assert "BLOCKED by tool_policy" in block
    assert "read_file" in block


def test_defer_rule_allows_existing_tool_approval_to_handle(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "tool_policy": {
                "enabled": True,
                "default_decision": "ask",
                "protected_by_existing_approval": ["terminal"],
                "rules": [],
            }
        },
    )

    assert enforce_tool_policy("terminal", {"command": "rm -rf /tmp/x"}) is None


def test_unknown_tool_uses_unknown_decision():
    decision = evaluate_tool_policy(
        "new_plugin_tool",
        {},
        {"enabled": True, "unknown_tools": "ask", "default_decision": "allow", "rules": []},
    )

    assert decision.decision == "ask"
    assert decision.policy_key == "tool_policy:unknown:new_plugin_tool"


def test_capability_rule_matches_known_tool():
    decision = evaluate_tool_policy(
        "send_message",
        {"message": "hello"},
        {
            "enabled": True,
            "default_decision": "allow",
            "rules": [
                {"name": "ask_outbound", "capability": "communication.send", "decision": "ask"}
            ],
        },
    )

    assert decision.decision == "ask"
    assert decision.rule_name == "ask_outbound"


def test_ask_approval_blocks_when_denied(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "tool_policy": {
                "enabled": True,
                "rules": [{"name": "ask_writes", "tools": ["write_file"], "decision": "ask"}],
            }
        },
    )
    monkeypatch.setattr(
        "tools.approval.request_user_approval",
        lambda *a, **kw: {"approved": False, "message": "BLOCKED: denied"},
    )

    assert enforce_tool_policy("write_file", {"path": "x.txt", "content": "hi"}) == "BLOCKED: denied"


def test_ask_approval_allows_when_approved(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "tool_policy": {
                "enabled": True,
                "rules": [{"name": "ask_writes", "tools": ["write_file"], "decision": "ask"}],
            }
        },
    )
    monkeypatch.setattr(
        "tools.approval.request_user_approval",
        lambda *a, **kw: {"approved": True, "message": None},
    )

    assert enforce_tool_policy("write_file", {"path": "x.txt", "content": "hi"}) is None


def test_pre_tool_call_integration_blocks_before_plugin_hooks(monkeypatch):
    hook_calls = []

    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "tool_policy": {
                "enabled": True,
                "rules": [{"name": "deny_writes", "tools": ["write_file"], "decision": "deny"}],
            }
        },
    )
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *a, **kw: hook_calls.append(a) or [])

    block = get_pre_tool_call_block_message("write_file", {"path": "x.txt"})

    assert "tool_policy" in block
    assert hook_calls == []
