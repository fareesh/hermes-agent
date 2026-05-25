from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agent.model_policy import (
    apply_model_policy,
    apply_policy_for_turn,
    build_policy_context,
    select_model_policy,
)
from hermes_cli.config import validate_config_structure


def test_policy_disabled_no_selection():
    cfg = {"model_policy": {"enabled": False, "rules": [
        {"when": {"platform": "email"}, "use": {"provider": "openrouter", "model": "cheap"}}
    ]}}

    assert select_model_policy(cfg, {"platform": "email"}) is None


def test_first_matching_rule_wins_for_email():
    cfg = {"model_policy": {"enabled": True, "rules": [
        {
            "name": "cheap_email",
            "when": {"platform": "email"},
            "use": {"provider": "openrouter", "model": "google/gemini-3-flash-preview"},
        },
        {
            "name": "vip_email",
            "when": {"platform": "email", "user_id_regex": "@vip\\.test$"},
            "use": {"provider": "anthropic", "model": "claude-sonnet-4-6"},
        },
    ]}}

    selected = select_model_policy(cfg, {"platform": "email", "user_id": "ceo@vip.test"})

    assert selected["rule_name"] == "cheap_email"
    assert selected["provider"] == "openrouter"
    assert selected["scope"] == "session"
    assert selected["on_unavailable"] == "error"


def test_turn_scope_is_explicit():
    cfg = {"model_policy": {"enabled": True, "rules": [
        {
            "name": "cheap_summary",
            "scope": "turn",
            "when": {"message_regex": "summary"},
            "use": {"provider": "openrouter", "model": "cheap"},
        }
    ]}}

    selected = select_model_policy(cfg, {"message_text": "write a summary"})

    assert selected["scope"] == "turn"
    assert selected["on_unavailable"] == "skip"


def test_channel_alias_predicates_match_chat_fields():
    agent = SimpleNamespace(
        platform="slack",
        _chat_type="channel",
        _chat_id="C123",
        _chat_name="alerts",
        _thread_id="",
        _user_id="U1",
        _user_name="Ada",
    )
    ctx = build_policy_context(agent, "summarize this", [])
    cfg = {"model_policy": {"enabled": True, "rules": [
        {
            "name": "alerts_channel",
            "when": {
                "platform": ["slack", "discord"],
                "chat_type": "channel",
                "channel_id": "C123",
                "channel_name_regex": "^alerts$",
            },
            "use": {"provider": "openrouter", "model": "cheap"},
        }
    ]}}

    selected = select_model_policy(cfg, ctx)

    assert selected["rule_name"] == "alerts_channel"


def test_message_and_image_predicates():
    ctx = {
        "platform": "discord",
        "message_text": "please refactor this",
        "has_image": True,
        "history_messages": 4,
    }
    cfg = {"model_policy": {"enabled": True, "rules": [
        {
            "name": "vision_coding",
            "when": {
                "message_regex": "(?i)refactor",
                "has_image": True,
                "min_history_messages": 3,
            },
            "use": {"provider": "gemini", "model": "gemini-3-pro-preview"},
        }
    ]}}

    assert select_model_policy(cfg, ctx)["rule_name"] == "vision_coding"


def test_apply_policy_uses_existing_switch_and_preserves_fallback_chain():
    calls = []
    fallback_chain = [{"provider": "anthropic", "model": "claude-sonnet-4-6"}]

    def switch_model(model, provider, api_key="", base_url="", api_mode=""):
        calls.append((model, provider, api_key, base_url, api_mode))
        agent.model = model
        agent.provider = provider
        agent.base_url = base_url
        agent.api_mode = api_mode

    agent = SimpleNamespace(
        model="default-model",
        provider="openrouter",
        base_url="https://openrouter.ai/api/v1",
        api_mode="chat_completions",
        _primary_runtime={
            "model": "default-model",
            "provider": "openrouter",
            "base_url": "https://openrouter.ai/api/v1",
            "api_mode": "chat_completions",
            "api_key": "primary-key",
        },
        _fallback_chain=list(fallback_chain),
        _fallback_model=fallback_chain[0],
        _model_policy_default_runtime=None,
        _model_policy_active=False,
        _model_policy_scope=None,
        switch_model=switch_model,
    )
    client = MagicMock()
    client.base_url = "https://api.example.test/v1"
    client.api_key = "policy-key"

    with patch("agent.auxiliary_client.resolve_provider_client", return_value=(client, "cheap")):
        result = apply_model_policy(agent, {
            "rule_name": "cheap_email",
            "provider": "custom",
            "model": "cheap",
            "base_url": "https://api.example.test/v1",
        })

    assert result.status == "applied"
    assert calls[0][0:4] == ("cheap", "custom", "policy-key", "https://api.example.test/v1")
    assert agent._fallback_chain == fallback_chain
    assert agent._model_policy_rule == "cheap_email"
    assert agent._model_policy_scope == "session"


def test_apply_policy_restores_default_after_unmatched_policy_turn():
    calls = []

    def switch_model(model, provider, api_key="", base_url="", api_mode=""):
        calls.append((model, provider, api_key, base_url, api_mode))
        agent.model = model
        agent.provider = provider

    agent = SimpleNamespace(
        model="cheap",
        provider="custom",
        _primary_runtime={},
        _model_policy_default_runtime={
            "model": "default-model",
            "provider": "openrouter",
            "base_url": "https://openrouter.ai/api/v1",
            "api_mode": "chat_completions",
            "api_key": "primary-key",
        },
        _model_policy_active=True,
        _model_policy_scope="turn",
        _fallback_chain=[],
        switch_model=switch_model,
    )

    result = apply_model_policy(agent, None)
    assert result.status == "restored"
    assert calls == [("default-model", "openrouter", "primary-key", "https://openrouter.ai/api/v1", "chat_completions")]
    assert agent._model_policy_active is False


def test_unmatched_policy_does_not_restore_session_scoped_route():
    calls = []

    def switch_model(model, provider, api_key="", base_url="", api_mode=""):
        calls.append((model, provider, api_key, base_url, api_mode))

    agent = SimpleNamespace(
        model="private-model",
        provider="lmstudio",
        _primary_runtime={},
        _model_policy_default_runtime={
            "model": "default-model",
            "provider": "openrouter",
            "base_url": "https://openrouter.ai/api/v1",
            "api_mode": "chat_completions",
            "api_key": "primary-key",
        },
        _model_policy_active=True,
        _model_policy_scope="session",
        _fallback_chain=[],
        switch_model=switch_model,
    )

    result = apply_model_policy(agent, None)
    assert result.status == "noop"
    assert calls == []
    assert agent.model == "private-model"


def test_session_scoped_policy_is_not_reevaluated_each_turn():
    agent = SimpleNamespace(_model_policy_active=True, _model_policy_scope="session")
    cfg = {"model_policy": {"enabled": True, "rules": [
        {"when": {"platform": "email"}, "use": {"provider": "openrouter", "model": "cheap"}}
    ]}}

    assert apply_policy_for_turn(agent, cfg, "hello", []).status == "noop"


def test_session_policy_missing_provider_blocks_by_default():
    agent = SimpleNamespace(
        model="default-model",
        provider="openrouter",
        _primary_runtime={"model": "default-model", "provider": "openrouter"},
        _model_policy_default_runtime=None,
        _model_policy_active=False,
    )

    with patch("agent.auxiliary_client.resolve_provider_client", return_value=(None, None)):
        result = apply_model_policy(agent, {
            "rule_name": "private_email",
            "scope": "session",
            "provider": "lmstudio",
            "model": "local-private-model",
        })

    assert result.blocked is True
    assert "private_email" in result.message
    assert "lmstudio" in result.message


def test_turn_policy_missing_provider_skips_by_default():
    agent = SimpleNamespace(
        model="default-model",
        provider="openrouter",
        _primary_runtime={"model": "default-model", "provider": "openrouter"},
        _model_policy_default_runtime=None,
        _model_policy_active=False,
    )

    with patch("agent.auxiliary_client.resolve_provider_client", return_value=(None, None)):
        result = apply_model_policy(agent, {
            "rule_name": "cheap_summary",
            "scope": "turn",
            "provider": "openrouter",
            "model": "cheap-model",
        })

    assert result.status == "skipped"
    assert result.blocked is False


def test_session_policy_can_explicitly_skip_unavailable_provider():
    agent = SimpleNamespace(
        model="default-model",
        provider="openrouter",
        _primary_runtime={"model": "default-model", "provider": "openrouter"},
        _model_policy_default_runtime=None,
        _model_policy_active=False,
    )

    with patch("agent.auxiliary_client.resolve_provider_client", return_value=(None, None)):
        result = apply_model_policy(agent, {
            "rule_name": "optional_session_route",
            "scope": "session",
            "on_unavailable": "skip",
            "provider": "anthropic",
            "model": "claude-sonnet-4-6",
        })

    assert result.status == "skipped"
    assert result.blocked is False


def test_model_policy_validation_warns_on_bad_rule_shape():
    issues = validate_config_structure({
        "model_policy": {
            "enabled": True,
            "rules": [
                {
                    "scope": "forever",
                    "on_unavailable": "explode",
                    "when": {"unknown_predicate": True},
                    "use": {"provider": "openrouter"},
                },
            ],
        }
    })

    messages = [issue.message for issue in issues]
    assert any("unknown predicate" in message for message in messages)
    assert any("missing 'model'" in message for message in messages)
    assert any("scope should be" in message for message in messages)
    assert any("on_unavailable should be" in message for message in messages)
