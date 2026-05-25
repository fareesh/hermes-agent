"""Deterministic opt-in policy gate for tool calls.

The policy is disabled by default. When enabled, it evaluates the concrete
tool name, known capabilities, and selected arguments before a tool executes.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional


VALID_DECISIONS = {"allow", "ask", "deny", "defer"}


TOOL_CAPABILITIES: dict[str, list[str]] = {
    "send_message": ["communication.send"],
    "yb_send_dm": ["communication.send"],
    "yb_send_sticker": ["communication.send"],
    "discord": ["communication.send"],
    "discord_admin": ["communication.admin"],
    "write_file": ["filesystem.write"],
    "patch": ["filesystem.write"],
    "terminal": ["shell.execute"],
    "process": ["process.manage"],
    "browser_click": ["browser.act"],
    "browser_type": ["browser.act"],
    "browser_press": ["browser.act"],
    "browser_scroll": ["browser.act"],
    "browser_dialog": ["browser.act"],
    "browser_cdp": ["browser.act"],
    "cronjob": ["automation.schedule"],
    "memory": ["memory.access"],
    "todo": ["memory.access"],
    "skill_manage": ["skill.modify"],
}


@dataclass(frozen=True)
class ToolPolicyDecision:
    decision: str
    reason: str = ""
    policy_key: str = ""
    rule_name: str = ""
    explicit_rule: bool = False


def enforce_tool_policy(
    tool_name: str,
    args: Optional[Dict[str, Any]],
    *,
    task_id: str = "",
    session_id: str = "",
    tool_call_id: str = "",
) -> Optional[str]:
    """Return a block message when policy denies a tool call, else ``None``."""
    config = _load_policy_config()
    if not isinstance(config, dict) or not config.get("enabled", False):
        return None

    safe_args = args if isinstance(args, dict) else {}
    decision = evaluate_tool_policy(tool_name, safe_args, config)
    if decision.decision in {"allow", "defer"}:
        return None
    if decision.decision == "deny":
        return _deny_message(tool_name, decision)
    if decision.decision == "ask":
        return _ask_for_approval(
            tool_name,
            safe_args,
            decision,
            task_id=task_id,
            session_id=session_id,
            tool_call_id=tool_call_id,
        )
    return None


def evaluate_tool_policy(
    tool_name: str,
    args: Dict[str, Any],
    policy_config: Dict[str, Any],
) -> ToolPolicyDecision:
    capabilities = set(get_tool_capabilities(tool_name))
    rules = policy_config.get("rules") or []
    if not isinstance(rules, list):
        rules = []

    for index, rule in enumerate(rules):
        if not isinstance(rule, dict):
            continue
        if not _rule_matches(rule, tool_name, capabilities, args):
            continue
        decision = _normalize_decision(rule.get("decision", rule.get("action", "allow")))
        name = str(rule.get("name") or f"rule_{index}")
        reason = str(rule.get("reason") or rule.get("message") or f"matched rule '{name}'")
        return ToolPolicyDecision(
            decision=decision,
            reason=reason,
            policy_key=f"tool_policy:{name}",
            rule_name=name,
            explicit_rule=True,
        )

    protected = _as_string_set(policy_config.get("protected_by_existing_approval"))
    if tool_name in protected:
        return ToolPolicyDecision(
            decision="defer",
            reason=f"{tool_name} has existing approval handling",
            policy_key=f"tool_policy:protected:{tool_name}",
        )

    if not capabilities:
        decision = _normalize_decision(policy_config.get("unknown_tools", "allow"))
        return ToolPolicyDecision(
            decision=decision,
            reason=f"unknown tool '{tool_name}'",
            policy_key=f"tool_policy:unknown:{tool_name}",
        )

    decision = _normalize_decision(policy_config.get("default_decision", "allow"))
    return ToolPolicyDecision(
        decision=decision,
        reason="default tool policy decision",
        policy_key=f"tool_policy:default:{tool_name}",
    )


def get_tool_capabilities(tool_name: str) -> list[str]:
    return list(TOOL_CAPABILITIES.get(tool_name, []))


def _ask_for_approval(
    tool_name: str,
    args: Dict[str, Any],
    decision: ToolPolicyDecision,
    *,
    task_id: str,
    session_id: str,
    tool_call_id: str,
) -> Optional[str]:
    from tools.approval import request_user_approval

    try:
        from tools.terminal_tool import _get_approval_callback
        approval_callback = _get_approval_callback()
    except Exception:
        approval_callback = None

    subject = _format_subject(tool_name, args)
    description = decision.reason or f"tool policy requires approval for {tool_name}"
    result = request_user_approval(
        subject,
        description,
        decision.policy_key or f"tool_policy:{tool_name}",
        allow_permanent=False,
        approval_callback=approval_callback,
        surface_label="tool_policy",
    )
    if result.get("approved"):
        return None
    return result.get("message") or _deny_message(tool_name, decision)


def _deny_message(tool_name: str, decision: ToolPolicyDecision) -> str:
    reason = decision.reason or "blocked by tool policy"
    return f"BLOCKED by tool_policy: {tool_name} was denied ({reason})."


def _format_subject(tool_name: str, args: Dict[str, Any]) -> str:
    preview = json.dumps(args, ensure_ascii=False, sort_keys=True)
    if len(preview) > 1200:
        preview = preview[:1197] + "..."
    return f"tool: {tool_name}\nargs: {preview}"


def _rule_matches(
    rule: Dict[str, Any],
    tool_name: str,
    capabilities: set[str],
    args: Dict[str, Any],
) -> bool:
    tool_selector = rule.get("tools", rule.get("tool"))
    capability_selector = rule.get("capabilities", rule.get("capability"))

    if tool_selector is not None and tool_name not in _as_string_set(tool_selector):
        return False
    if capability_selector is not None:
        wanted = _as_string_set(capability_selector)
        if not capabilities.intersection(wanted):
            return False
    if tool_selector is None and capability_selector is None and "when" not in rule:
        return False

    predicates = rule.get("when") or {}
    if predicates is None:
        return True
    if not isinstance(predicates, dict):
        return False
    return all(_match_arg_predicate(args.get(field), predicate) for field, predicate in predicates.items())


def _match_arg_predicate(value: Any, predicate: Any) -> bool:
    if isinstance(predicate, dict):
        for op, expected in predicate.items():
            if not _match_operator(value, str(op), expected):
                return False
        return True
    return value == predicate


def _match_operator(value: Any, op: str, expected: Any) -> bool:
    text = "" if value is None else str(value)
    if op == "equals":
        return value == expected or text == str(expected)
    if op == "contains":
        return str(expected) in text
    if op == "prefix":
        return text.startswith(str(expected))
    if op == "suffix":
        return text.endswith(str(expected))
    if op == "regex":
        try:
            return re.search(str(expected), text) is not None
        except re.error:
            return False
    if op == "in":
        if not isinstance(expected, Iterable) or isinstance(expected, (str, bytes, dict)):
            return False
        return value in expected or text in {str(item) for item in expected}
    return False


def _as_string_set(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        return {value}
    if isinstance(value, Iterable) and not isinstance(value, (bytes, dict)):
        return {str(item) for item in value}
    return {str(value)}


def _normalize_decision(value: Any) -> str:
    decision = str(value or "allow").strip().lower()
    return decision if decision in VALID_DECISIONS else "allow"


def _load_policy_config() -> Dict[str, Any]:
    try:
        from hermes_cli.config import load_config
        config = load_config()
    except Exception:
        return {}
    policy = config.get("tool_policy") if isinstance(config, dict) else None
    return policy if isinstance(policy, dict) else {}
