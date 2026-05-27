"""Kimi/Moonshot Anthropic-wire endpoints now receive ``thinking`` parameter.

``copy_reasoning_content_for_api`` (run_agent.py) pads all assistant tool-call
messages with a ``reasoning_content`` placeholder, and
``convert_messages_to_anthropic`` (anthropic_adapter.py:1687) converts it to a
``thinking`` block — so ``thinking.enabled`` no longer 400s on missing
``reasoning_content``.  Per-turn thinking budget is controlled by
``reasoning_config`` (disabled for tool-call re-entries, enabled for fresh
user turns).
"""

from __future__ import annotations

import pytest


class TestKimiCodingSkipsAnthropicThinking:
    """build_anthropic_kwargs injects ``thinking`` for Kimi /coding when enabled."""

    @pytest.mark.parametrize(
        "base_url",
        [
            "https://api.kimi.com/coding",
            "https://api.kimi.com/coding/v1",
            "https://api.kimi.com/coding/anthropic",
            "https://api.kimi.com/coding/",
        ],
    )
    def test_kimi_coding_endpoint_receives_thinking(self, base_url: str) -> None:
        from agent.anthropic_adapter import build_anthropic_kwargs

        kwargs = build_anthropic_kwargs(
            model="kimi-k2.5",
            messages=[{"role": "user", "content": "hello"}],
            tools=None,
            max_tokens=4096,
            reasoning_config={"enabled": True, "effort": "medium"},
            base_url=base_url,
        )
        assert "thinking" in kwargs, (
            "Anthropic thinking must be sent to Kimi /coding when "
            "reasoning_config is enabled — copy_reasoning_content_for_api "
            "pads the history with reasoning_content placeholders."
        )

    def test_kimi_coding_with_explicit_disabled_omits_thinking(self) -> None:
        from agent.anthropic_adapter import build_anthropic_kwargs

        kwargs = build_anthropic_kwargs(
            model="kimi-k2.5",
            messages=[{"role": "user", "content": "hello"}],
            tools=None,
            max_tokens=4096,
            reasoning_config={"enabled": False},
            base_url="https://api.kimi.com/coding",
        )
        assert "thinking" not in kwargs

    def test_non_kimi_third_party_still_gets_thinking(self) -> None:
        """MiniMax and other third-party Anthropic endpoints must retain thinking."""
        from agent.anthropic_adapter import build_anthropic_kwargs

        kwargs = build_anthropic_kwargs(
            model="MiniMax-M2.7",
            messages=[{"role": "user", "content": "hello"}],
            tools=None,
            max_tokens=4096,
            reasoning_config={"enabled": True, "effort": "medium"},
            base_url="https://api.minimax.io/anthropic",
        )
        assert "thinking" in kwargs
        assert kwargs["thinking"]["type"] == "enabled"

    def test_native_anthropic_still_gets_thinking(self) -> None:
        from agent.anthropic_adapter import build_anthropic_kwargs

        kwargs = build_anthropic_kwargs(
            model="claude-sonnet-4-20250514",
            messages=[{"role": "user", "content": "hello"}],
            tools=None,
            max_tokens=4096,
            reasoning_config={"enabled": True, "effort": "medium"},
            base_url=None,
        )
        assert "thinking" in kwargs

    def test_kimi_root_endpoint_via_anthropic_transport_receives_thinking(self) -> None:
        """Plain ``api.kimi.com`` hit via the Anthropic transport also gets thinking.

        ``copy_reasoning_content_for_api`` pads reasoning_content placeholders
        on all Kimi hosts (not just /coding), so ``thinking.enabled`` won't
        400 on missing ``reasoning_content``.
        """
        from agent.anthropic_adapter import build_anthropic_kwargs

        kwargs = build_anthropic_kwargs(
            model="kimi-k2.5",
            messages=[{"role": "user", "content": "hello"}],
            tools=None,
            max_tokens=4096,
            reasoning_config={"enabled": True, "effort": "medium"},
            base_url="https://api.kimi.com/v1",
        )
        assert "thinking" in kwargs, (
            "Kimi via anthropic transport must receive thinking when "
            "reasoning_config is enabled."
        )

    # ── #17057: custom / proxied Kimi-compatible endpoints ──────────
    @pytest.mark.parametrize(
        "base_url,model",
        [
            # Custom host with Kimi-family model — the reporter's case
            ("http://my-kimi-proxy.internal", "kimi-2.6"),
            ("https://llm.example.com/anthropic", "kimi-k2.5"),
            ("https://llm.example.com/anthropic", "moonshot-v1-8k"),
            ("https://llm.example.com/anthropic", "kimi_thinking"),
            ("https://llm.example.com/anthropic", "moonshotai/kimi-k2.5"),
            # Official Moonshot host (previously uncovered)
            ("https://api.moonshot.ai/anthropic", "moonshot-v1-32k"),
            ("https://api.moonshot.cn/anthropic", "moonshot-v1-32k"),
        ],
    )
    def test_kimi_family_custom_endpoint_receives_thinking(
        self, base_url: str, model: str
    ) -> None:
        """Custom / proxied Kimi endpoints now receive Anthropic thinking."""
        from agent.anthropic_adapter import build_anthropic_kwargs

        kwargs = build_anthropic_kwargs(
            model=model,
            messages=[{"role": "user", "content": "hello"}],
            tools=None,
            max_tokens=4096,
            reasoning_config={"enabled": True, "effort": "medium"},
            base_url=base_url,
        )
        assert "thinking" in kwargs, (
            f"Kimi-family endpoint ({base_url}, {model}) must receive "
            f"Anthropic thinking when reasoning_config is enabled — "
            f"copy_reasoning_content_for_api pads the reasoning_content "
            f"placeholders."
        )

    def test_custom_endpoint_non_kimi_model_keeps_thinking(self) -> None:
        """Custom endpoint with a non-Kimi model must keep thinking intact.

        Guards against over-broad model-family matching — only model names
        starting with a Kimi/Moonshot prefix should trigger suppression.
        """
        from agent.anthropic_adapter import build_anthropic_kwargs

        kwargs = build_anthropic_kwargs(
            model="MiniMax-M2.7",
            messages=[{"role": "user", "content": "hello"}],
            tools=None,
            max_tokens=4096,
            reasoning_config={"enabled": True, "effort": "medium"},
            base_url="https://my-llm-proxy.example.com/anthropic",
        )
        assert "thinking" in kwargs
        assert kwargs["thinking"]["type"] == "enabled"

    def test_kimi_family_replay_preserves_unsigned_thinking(self) -> None:
        """On a custom Kimi endpoint, unsigned reasoning_content thinking
        blocks must survive the third-party signature-stripping pass so
        the upstream's message-history validation passes.
        """
        from agent.anthropic_adapter import convert_messages_to_anthropic

        messages = [
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "reasoning_content": "planning the tool call",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "skill_view", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
        ]
        _, converted = convert_messages_to_anthropic(
            messages,
            base_url="http://my-kimi-proxy.internal",
            model="kimi-2.6",
        )
        # The assistant message still carries the unsigned thinking block
        # synthesised from reasoning_content (required by Kimi's history
        # validation).  A plain third-party endpoint would have stripped it.
        assistant_msg = next(m for m in converted if m["role"] == "assistant")
        assistant_blocks = assistant_msg["content"]
        thinking_blocks = [
            b for b in assistant_blocks
            if isinstance(b, dict) and b.get("type") == "thinking"
        ]
        assert len(thinking_blocks) == 1
        assert thinking_blocks[0]["thinking"] == "planning the tool call"
