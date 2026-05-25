---
title: Model Policy Routing
description: Route main-agent sessions by platform or channel.
sidebar_label: Model Policy Routing
sidebar_position: 9
---

# Model Policy Routing

Model policy routing is an advanced, optional feature for users who run multiple model providers and want Hermes to choose the main model before requests are sent.

It is disabled by default. Use it when you want predictable rules such as routing incoming email, bulk notification channels, or sensitive workspaces to specific providers while keeping direct messages or VIP senders on stronger models.

:::warning History follows the selected model
LLM calls usually include conversation history. If a later turn switches a session to a commercial provider, that provider can receive earlier messages from the session. For that reason, policy rules are **session-scoped by default**. Use `scope: turn` only for explicit cost/performance routing where sharing the existing session history with the selected provider is acceptable.
:::

## Configuration

Add `model_policy` to `~/.hermes/config.yaml`:

```yaml
model_policy:
  enabled: true
  rules:
    - name: cheap_email
      scope: session       # default; shown for clarity
      on_unavailable: error # default for session rules
      when:
        platform: email
      use:
        provider: openrouter
        model: google/gemini-3-flash-preview

    - name: vip_email
      scope: session
      when:
        platform: email
        user_id_regex: "(?i)@(important-client\\.com)$"
      use:
        provider: anthropic
        model: claude-sonnet-4-6

    - name: cheap_bulk_channels
      scope: session
      when:
        platform: [slack, discord]
        chat_type: channel
        channel_name_regex: "^(alerts|triage|inbox)$"
      use:
        provider: openrouter
        model: google/gemini-3-flash-preview
```

Rules are evaluated in order. The first matching rule wins. If no rule matches, Hermes uses the configured default model. After a `scope: session` rule matches, Hermes keeps that route for the session instead of re-evaluating cheaper/stronger turn-level choices later.

## Scope

`scope` controls how long a matched rule applies:

| Scope | Behavior |
|---|---|
| `session` | Default. Selects the provider/model for the session and keeps using it on later turns. Use this for privacy or trust boundaries. |
| `turn` | Re-evaluates every turn and restores the default route when no rule matches. Use this only for cost/performance routing where sending existing history to the selected model is acceptable. |

Example explicit turn-scoped optimization:

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

## Unavailable Providers

When a matching rule points at a provider that is not configured, Hermes uses `on_unavailable` to decide what to do:

| Value | Behavior |
|---|---|
| `error` | Fail closed. Hermes returns an error before making any LLM call. |
| `skip` | Ignore the rule and continue with the current/default model. |

Defaults are scope-aware:

| Scope | Default `on_unavailable` |
|---|---|
| `session` | `error` |
| `turn` | `skip` |

This protects privacy-oriented session rules. For example, if email is supposed to route to a local model but that local provider is not configured, Hermes will not silently send the email session to the global default model.

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
```

## Predicates

Supported predicates:

| Predicate | Description |
|---|---|
| `platform` | Exact platform name or list, such as `email`, `slack`, `discord`, `cli`. |
| `chat_type` | Exact chat type: `dm`, `group`, `channel`, or `thread`. |
| `channel_id` | Exact channel/chat ID. Internally this maps to `chat_id`. |
| `channel_id_regex` | Regex matched against the channel/chat ID. |
| `channel_name` | Exact channel/chat name. Internally this maps to `chat_name`. |
| `channel_name_regex` | Regex matched against the channel/chat name. |
| `thread_id` | Exact thread/topic ID. |
| `thread_id_regex` | Regex matched against the thread/topic ID. |
| `user_id` | Exact platform user ID. For email this is the sender address. |
| `user_id_regex` | Regex matched against the platform user ID. |
| `user_name` | Exact platform user display name. |
| `user_name_regex` | Regex matched against the display name. |
| `message_regex` | Regex matched against the current user message. |
| `cwd_regex` | Regex matched against the current working directory. |
| `has_image` | Boolean; true when the current message contains native image parts. |
| `min_history_messages` | Minimum number of prior conversation messages. |

## How It Works

Model policy routing runs outside the model loop, before Hermes builds the LLM request. It does not change the system prompt and does not expose the policy contents to the model.

When a rule matches, Hermes reuses the existing provider resolution and live model-switching path. The selected provider/model becomes the primary runtime for the applicable scope, so existing fallback providers still work if the selected backend fails.

## Relationship To Other Routing

`model_policy` is proactive main-model selection. It chooses the provider/model before the LLM call.

`fallback_providers` is reactive failover. It activates only after the current provider fails.

`provider_routing` is OpenRouter-specific sub-provider routing. It controls which upstream OpenRouter provider handles an OpenRouter request.

`auxiliary.*` controls side-task models such as vision, compression, and web extraction. Model policy routing does not affect those auxiliary calls.

## Notes

Keep policy rules simple and order them from most specific to least specific. If a provider is not configured, Hermes logs a warning and continues with the current/default model.

For privacy-sensitive routing, prefer `scope: session` and start a new session when changing trust boundaries.
