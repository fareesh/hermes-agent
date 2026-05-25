# Model Policy Routing

## Summary

This change adds an optional, default-off `model_policy` configuration block for power users who run multiple model providers and want Hermes to select a main model based on session metadata such as platform, channel, sender, message shape, or working directory.

The feature is intentionally separate from existing routing systems:

- `provider_routing` still controls OpenRouter sub-provider preferences.
- `fallback_providers` still handles reactive failover after provider errors.
- `auxiliary.*` still controls side-task models such as vision, compression, and web extraction.
- `model_policy` proactively selects the main agent runtime before the LLM request is built.

## Motivation

The original motivation was cost control for high-volume inbound channels such as email, alerts, or triage feeds. Those channels often do not need the same expensive model as interactive coding or VIP direct messages.

During design review, the core issue became privacy and trust boundaries. Hermes sends conversation history to the active model. A naive per-turn router can accidentally disclose prior turns to a different provider if a later turn switches models. For that reason, policy routing now defaults to session scope rather than turn scope.

## Configuration

The default config is inert:

```yaml
model_policy:
  enabled: false
  rules: []
```

Example session-scoped routing:

```yaml
model_policy:
  enabled: true
  rules:
    - name: private_email_local
      scope: session
      on_unavailable: error
      when:
        platform: email
      use:
        provider: lmstudio
        model: local-private-model

    - name: slack_alerts_cheap
      scope: session
      when:
        platform: slack
        chat_type: channel
        channel_name_regex: "^(alerts|triage)$"
      use:
        provider: openrouter
        model: google/gemini-3-flash-preview
```

Example explicit turn-scoped cost optimization:

```yaml
model_policy:
  enabled: true
  rules:
    - name: cheap_summaries
      scope: turn
      on_unavailable: skip
      when:
        message_regex: "(?i)\\b(summarize|rewrite)\\b"
      use:
        provider: openrouter
        model: google/gemini-3-flash-preview
```

## Scope Semantics

`scope: session` is the default. Once a session-scoped rule matches, Hermes keeps that provider/model for the session and does not re-evaluate model policy on later turns. This is the safer behavior for privacy and trust boundaries because it avoids moving existing history to another provider later.

`scope: turn` must be explicit. Turn-scoped rules are re-evaluated each user turn and restore the default route when no rule matches. This is appropriate for cost/performance optimization only when sending existing conversation history to the selected model is acceptable.

## Unavailable Provider Semantics

Policy rules can set `on_unavailable`:

- `error`: fail closed and return before any LLM call.
- `skip`: ignore the rule and continue with the current/default route.

Defaults are scope-aware:

- `scope: session` defaults to `on_unavailable: error`.
- `scope: turn` defaults to `on_unavailable: skip`.

This avoids the unsafe case where a privacy-oriented session rule silently falls back to a commercial/global default provider because the intended private provider is not configured.

## Predicates

Supported predicates are deliberately simple and declarative:

- `platform`
- `chat_type`
- `channel_id`
- `channel_id_regex`
- `channel_name`
- `channel_name_regex`
- `thread_id`
- `thread_id_regex`
- `user_id`
- `user_id_regex`
- `user_name`
- `user_name_regex`
- `message_regex`
- `cwd_regex`
- `has_image`
- `min_history_messages`

`channel_id` and `channel_name` are user-facing aliases for the internal `chat_id` and `chat_name` fields. This makes Slack/Discord/email policy rules read naturally while still using the existing gateway `SessionSource` metadata.

## Implementation

The implementation is centered in `agent/model_policy.py`:

- `build_policy_context(...)` extracts platform/channel/user/message context from the live `AIAgent` instance.
- `select_model_policy(...)` returns the first matching rule target.
- `apply_model_policy(...)` resolves provider credentials through `agent.auxiliary_client.resolve_provider_client(...)` and applies the route through `agent.switch_model(...)`.
- `ModelPolicyResult` reports `applied`, `skipped`, `restored`, `blocked`, or `noop` status.

`agent/conversation_loop.py` evaluates policy at the top of `run_conversation()`, after fallback restoration and before DB session creation or model calls. If policy returns `blocked`, the conversation returns an error response immediately and no LLM request is sent.

`agent/agent_init.py` initializes policy state:

- `_model_policy_active`
- `_model_policy_rule`
- `_model_policy_scope`
- `_model_policy_default_runtime`

`hermes_cli/config.py` adds the default config and validation warnings for malformed rules, unknown predicates, invalid `scope`, and invalid `on_unavailable`.

## Onboarding And Provider Setup

This change does not modify first-run onboarding. `hermes setup` still configures one primary provider/model via the shared `hermes model` picker. Multiple providers can already be prepared through:

- `hermes auth add <provider>` for credentials.
- `hermes fallback add` for reactive fallback providers.
- `providers:` / `custom_providers:` in `config.yaml` for custom endpoints.

`model_policy` is a power-user feature and is configured manually. If a matching policy target is not configured, session rules fail closed by default and turn rules skip by default.

Future work could add `hermes model-policy validate` or `hermes model-policy setup` to enumerate policy targets, check credentials, and offer to run the relevant auth flows.

## Cache And Prompt Safety

Policy evaluation happens in host code before request construction. It does not change the system prompt, toolsets, memory loading, or past messages. This preserves the existing prompt-cache invariants better than a prompt-level instruction would.

Session-scoped policy avoids repeated cross-provider switching once a boundary has been selected. Turn-scoped policy remains explicit and documented as history-sharing behavior.

## Tests

New tests live in `tests/agent/test_model_policy.py` and cover:

- disabled policy no-op
- first-match rule selection
- default `session` scope
- explicit `turn` scope
- channel alias predicates
- message/image/history predicates
- runtime application via existing switch path
- fallback chain preservation
- turn-scope default restoration
- session-scope stickiness
- session unavailable provider fail-closed behavior
- turn unavailable provider fail-open behavior
- explicit session `on_unavailable: skip`
- config validation warnings

The repository test wrapper could not run in this checkout because no `.venv` or `venv` exists. Syntax checks passed for changed Python files.

## Risks And Tradeoffs

The feature is default-off and should not affect existing users.

The biggest risk is user misunderstanding. The documentation explicitly warns that history follows the selected model and that `scope: turn` should not be treated as a privacy boundary.

Another risk is unavailable-provider behavior. The fail-closed default for session rules is conservative and may surprise users who expected fallback. Users can explicitly set `on_unavailable: skip` when they want fail-open behavior.

## Future Work

- Add `hermes model-policy validate`.
- Add `hermes model-policy setup` for advanced guided provider setup.
- Add named trust tiers or provider allowlists for stronger privacy policy expression.
- Surface policy-blocked errors in gateway logs/status with actionable setup hints.
