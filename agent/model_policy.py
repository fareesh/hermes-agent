"""Declarative main model policy routing.

This module is intentionally small and data-driven.  It evaluates a
default-off ``model_policy`` config block before the agent builds an API
request, then reuses the existing provider resolver and live model-switch
path to apply the selected provider/model.

Rules are session-scoped by default.  A later model switch receives the
conversation history, so per-turn routing is only appropriate when the user
explicitly opts into ``scope: turn`` for cost/performance optimization.
"""

from __future__ import annotations

import copy
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from utils import base_url_host_matches, base_url_hostname

logger = logging.getLogger(__name__)


@dataclass
class ModelPolicyResult:
    status: str = "noop"  # applied | skipped | restored | blocked | noop
    rule_name: str = ""
    provider: str = ""
    model: str = ""
    message: str = ""

    @property
    def applied(self) -> bool:
        return self.status in {"applied", "restored"}

    @property
    def blocked(self) -> bool:
        return self.status == "blocked"


_KNOWN_PREDICATES = frozenset({
    "platform",
    "chat_type",
    "channel_id",
    "channel_id_regex",
    "channel_name",
    "channel_name_regex",
    "thread_id",
    "thread_id_regex",
    "user_id",
    "user_id_regex",
    "user_name",
    "user_name_regex",
    "message_regex",
    "cwd_regex",
    "has_image",
    "min_history_messages",
})


def _as_list(value: Any) -> List[str]:
    if isinstance(value, (list, tuple, set)):
        return [str(v).strip().lower() for v in value if str(v).strip()]
    if value is None:
        return []
    text = str(value).strip()
    return [text.lower()] if text else []


def _exact_match(actual: Any, expected: Any) -> bool:
    actual_norm = str(actual or "").strip().lower()
    expected_values = _as_list(expected)
    return bool(actual_norm and expected_values and actual_norm in expected_values)


def _regex_match(actual: Any, pattern: Any) -> bool:
    text = str(actual or "")
    if not text or not isinstance(pattern, str) or not pattern.strip():
        return False
    try:
        return re.search(pattern, text) is not None
    except re.error as exc:
        logger.warning("Ignoring invalid model_policy regex %r: %s", pattern, exc)
        return False


def _content_has_image_parts(value: Any) -> bool:
    if isinstance(value, dict):
        ptype = value.get("type")
        if ptype in {"image_url", "input_image"}:
            return True
        return any(_content_has_image_parts(v) for v in value.values())
    if isinstance(value, list):
        return any(_content_has_image_parts(item) for item in value)
    return False


def build_policy_context(
    agent,
    user_message: Any,
    conversation_history: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Build the predicate context for a single user turn."""
    chat_id = getattr(agent, "_chat_id", None) or getattr(agent, "chat_id", "")
    chat_name = getattr(agent, "_chat_name", None) or getattr(agent, "chat_name", "")
    return {
        "platform": getattr(agent, "platform", "") or "",
        "chat_type": getattr(agent, "_chat_type", None) or getattr(agent, "chat_type", "") or "",
        "chat_id": chat_id or "",
        "channel_id": chat_id or "",
        "chat_name": chat_name or "",
        "channel_name": chat_name or "",
        "thread_id": getattr(agent, "_thread_id", None) or getattr(agent, "thread_id", "") or "",
        "user_id": getattr(agent, "_user_id", None) or getattr(agent, "user_id", "") or "",
        "user_name": getattr(agent, "_user_name", None) or getattr(agent, "user_name", "") or "",
        "message": user_message,
        "message_text": user_message if isinstance(user_message, str) else str(user_message or ""),
        "cwd": os.getcwd(),
        "has_image": _content_has_image_parts(user_message),
        "history_messages": len(conversation_history or []),
    }


def _rule_matches(rule: Dict[str, Any], context: Dict[str, Any]) -> bool:
    when = rule.get("when")
    if not isinstance(when, dict):
        return False

    for key, expected in when.items():
        if key not in _KNOWN_PREDICATES:
            logger.debug("Ignoring unknown model_policy predicate: %s", key)
            return False
        if key == "platform" and not _exact_match(context.get("platform"), expected):
            return False
        if key == "chat_type" and not _exact_match(context.get("chat_type"), expected):
            return False
        if key == "channel_id" and not _exact_match(context.get("channel_id"), expected):
            return False
        if key == "channel_name" and not _exact_match(context.get("channel_name"), expected):
            return False
        if key == "thread_id" and not _exact_match(context.get("thread_id"), expected):
            return False
        if key == "user_id" and not _exact_match(context.get("user_id"), expected):
            return False
        if key == "user_name" and not _exact_match(context.get("user_name"), expected):
            return False
        if key == "channel_id_regex" and not _regex_match(context.get("channel_id"), expected):
            return False
        if key == "channel_name_regex" and not _regex_match(context.get("channel_name"), expected):
            return False
        if key == "thread_id_regex" and not _regex_match(context.get("thread_id"), expected):
            return False
        if key == "user_id_regex" and not _regex_match(context.get("user_id"), expected):
            return False
        if key == "user_name_regex" and not _regex_match(context.get("user_name"), expected):
            return False
        if key == "message_regex" and not _regex_match(context.get("message_text"), expected):
            return False
        if key == "cwd_regex" and not _regex_match(context.get("cwd"), expected):
            return False
        if key == "has_image" and bool(context.get("has_image")) is not bool(expected):
            return False
        if key == "min_history_messages":
            try:
                minimum = int(expected)
            except (TypeError, ValueError):
                return False
            if int(context.get("history_messages") or 0) < minimum:
                return False

    return True


def select_model_policy(config: Dict[str, Any], context: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return the first matching policy target, or None when disabled/unmatched."""
    policy = config.get("model_policy") if isinstance(config, dict) else None
    if not isinstance(policy, dict) or not policy.get("enabled"):
        return None
    rules = policy.get("rules")
    if not isinstance(rules, list):
        return None

    for idx, rule in enumerate(rules):
        if not isinstance(rule, dict):
            continue
        target = rule.get("use")
        if not isinstance(target, dict):
            continue
        provider = str(target.get("provider") or "").strip()
        model = str(target.get("model") or "").strip()
        if not provider or not model:
            continue
        if _rule_matches(rule, context):
            selected = dict(target)
            selected["rule_name"] = str(rule.get("name") or f"rule_{idx}")
            scope = str(rule.get("scope") or target.get("scope") or "session").strip().lower()
            selected["scope"] = scope if scope in {"session", "turn"} else "session"
            on_unavailable = str(
                rule.get("on_unavailable")
                or target.get("on_unavailable")
                or ""
            ).strip().lower()
            if on_unavailable not in {"error", "skip"}:
                on_unavailable = "error" if selected["scope"] == "session" else "skip"
            selected["on_unavailable"] = on_unavailable
            return selected
    return None


def _snapshot_primary_runtime(agent) -> Dict[str, Any]:
    return copy.deepcopy(getattr(agent, "_primary_runtime", {}) or {})


def ensure_policy_default_runtime(agent) -> None:
    if not getattr(agent, "_model_policy_default_runtime", None):
        agent._model_policy_default_runtime = _snapshot_primary_runtime(agent)


def _restore_policy_default(agent) -> ModelPolicyResult:
    snapshot = getattr(agent, "_model_policy_default_runtime", None)
    if not isinstance(snapshot, dict) or not snapshot:
        return ModelPolicyResult()
    if not getattr(agent, "_model_policy_active", False):
        return ModelPolicyResult()
    if getattr(agent, "_model_policy_scope", "session") != "turn":
        return ModelPolicyResult()

    fallback_chain = list(getattr(agent, "_fallback_chain", []) or [])
    try:
        agent.switch_model(
            snapshot.get("model", ""),
            snapshot.get("provider", ""),
            api_key=snapshot.get("api_key", ""),
            base_url=snapshot.get("base_url", ""),
            api_mode=snapshot.get("api_mode", ""),
        )
        agent._fallback_chain = fallback_chain
        agent._fallback_model = fallback_chain[0] if fallback_chain else None
        agent._model_policy_active = False
        agent._model_policy_rule = None
        agent._model_policy_scope = None
        logger.info(
            "model_policy restored default route: %s via %s",
            agent.model,
            agent.provider or "unknown",
        )
        return ModelPolicyResult(
            status="restored",
            provider=agent.provider or "",
            model=agent.model or "",
            message="Restored default model route after turn-scoped policy.",
        )
    except Exception as exc:
        logger.warning("Failed to restore model_policy default route: %s", exc)
        return ModelPolicyResult(status="skipped", message=str(exc))


def _api_mode_for_target(agent, provider: str, model: str, base_url: str) -> str:
    if provider == "openai-codex":
        return "codex_responses"
    if provider == "anthropic" or str(base_url or "").rstrip("/").lower().endswith("/anthropic"):
        return "anthropic_messages"
    if provider == "bedrock" or (
        base_url_hostname(base_url).startswith("bedrock-runtime.")
        and base_url_host_matches(base_url, "amazonaws.com")
    ):
        return "bedrock_converse"
    try:
        if agent._is_azure_openai_url(base_url):
            return "chat_completions"
        if agent._is_direct_openai_url(base_url):
            return "codex_responses"
        if agent._provider_model_requires_responses_api(model, provider=provider):
            return "codex_responses"
    except Exception:
        pass
    try:
        from hermes_cli.providers import determine_api_mode
        return determine_api_mode(provider, base_url)
    except Exception:
        return "chat_completions"


def _unavailable_result(agent, *, rule_name: str, provider: str, model: str, scope: str, on_unavailable: str) -> ModelPolicyResult:
    message = (
        f"Model policy rule '{rule_name}' matched, but provider '{provider}' "
        f"for model '{model}' is not configured. Configure the provider or "
        "disable/update the rule."
    )
    if on_unavailable == "error":
        logger.warning("model_policy blocked: %s", message)
        return ModelPolicyResult(
            status="blocked",
            rule_name=rule_name,
            provider=provider,
            model=model,
            message=message,
        )
    logger.warning("model_policy rule %s skipped: provider %s is not configured", rule_name, provider)
    restored = _restore_policy_default(agent)
    if restored.applied:
        return restored
    return ModelPolicyResult(
        status="skipped",
        rule_name=rule_name,
        provider=provider,
        model=model,
        message=message,
    )


def apply_model_policy(agent, target: Optional[Dict[str, Any]]) -> ModelPolicyResult:
    """Apply a selected policy target, or restore default when unmatched."""
    ensure_policy_default_runtime(agent)
    if not target:
        return _restore_policy_default(agent)

    provider = str(target.get("provider") or "").strip().lower()
    model = str(target.get("model") or "").strip()
    rule_name = str(target.get("rule_name") or "").strip() or "unnamed"
    scope = str(target.get("scope") or "session").strip().lower()
    if scope not in {"session", "turn"}:
        scope = "session"
    on_unavailable = str(target.get("on_unavailable") or "").strip().lower()
    if on_unavailable not in {"error", "skip"}:
        on_unavailable = "error" if scope == "session" else "skip"
    if not provider or not model:
        return ModelPolicyResult(status="skipped", rule_name=rule_name)
    if (
        provider == (getattr(agent, "provider", "") or "").strip().lower()
        and model == getattr(agent, "model", "")
    ):
        agent._model_policy_active = True
        agent._model_policy_rule = rule_name
        agent._model_policy_scope = scope
        return ModelPolicyResult(
            status="noop",
            rule_name=rule_name,
            provider=provider,
            model=model,
        )

    try:
        from agent.auxiliary_client import resolve_provider_client

        base_url_hint = str(target.get("base_url") or "").strip() or None
        api_key_hint = str(target.get("api_key") or "").strip() or None
        if not api_key_hint:
            key_env = str(target.get("key_env") or target.get("api_key_env") or "").strip()
            if key_env:
                api_key_hint = os.getenv(key_env, "").strip() or None

        client, _resolved_model = resolve_provider_client(
            provider,
            model=model,
            raw_codex=True,
            explicit_base_url=base_url_hint,
            explicit_api_key=api_key_hint,
        )
        if client is None:
            return _unavailable_result(
                agent,
                rule_name=rule_name,
                provider=provider,
                model=model,
                scope=scope,
                on_unavailable=on_unavailable,
            )

        try:
            from hermes_cli.model_normalize import normalize_model_for_provider
            model = normalize_model_for_provider(model, provider)
        except Exception:
            pass

        base_url = str(getattr(client, "base_url", "") or base_url_hint or "")
        api_key = getattr(client, "api_key", "") or api_key_hint or ""
        api_mode = _api_mode_for_target(agent, provider, model, base_url)

        fallback_chain = list(getattr(agent, "_fallback_chain", []) or [])
        agent.switch_model(
            model,
            provider,
            api_key=api_key,
            base_url=base_url,
            api_mode=api_mode,
        )
        # Policy routing should not prune the user-configured fallback chain.
        agent._fallback_chain = fallback_chain
        agent._fallback_model = fallback_chain[0] if fallback_chain else None
        agent._model_policy_active = True
        agent._model_policy_rule = rule_name
        agent._model_policy_scope = scope
        logger.info("model_policy selected rule %s: %s via %s", rule_name, model, provider)
        return ModelPolicyResult(
            status="applied",
            rule_name=rule_name,
            provider=provider,
            model=model,
        )
    except Exception as exc:
        logger.warning("model_policy rule %s failed: %s", rule_name, exc)
        return _unavailable_result(
            agent,
            rule_name=rule_name,
            provider=provider,
            model=model,
            scope=scope,
            on_unavailable=on_unavailable,
        )


def apply_policy_for_turn(agent, config: Dict[str, Any], user_message: Any, conversation_history=None) -> ModelPolicyResult:
    if (
        getattr(agent, "_model_policy_active", False)
        and getattr(agent, "_model_policy_scope", "session") == "session"
    ):
        return ModelPolicyResult()
    context = build_policy_context(agent, user_message, conversation_history)
    target = select_model_policy(config, context)
    return apply_model_policy(agent, target)


__all__ = [
    "apply_model_policy",
    "apply_policy_for_turn",
    "build_policy_context",
    "ensure_policy_default_runtime",
    "ModelPolicyResult",
    "select_model_policy",
]
