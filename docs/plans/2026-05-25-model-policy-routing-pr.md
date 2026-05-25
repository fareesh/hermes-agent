# PR: Add Declarative Model Policy Routing

## What does this PR do?

Adds an optional, default-off `model_policy` config section for advanced users who want Hermes to choose the main agent provider/model from declarative rules.

The feature is designed around session-level trust boundaries. Rules default to `scope: session`, so once a session matches a policy route, Hermes keeps that provider/model for later turns instead of moving existing conversation history to another provider. Explicit `scope: turn` is available for cost/performance routing where sharing existing history with the selected model is acceptable.

Session-scoped rules fail closed by default when their target provider is not configured. Turn-scoped rules fail open by default and skip unavailable providers.

## Related Issue

N/A

## Type of Change

- [ ] Bug fix
- [x] New feature
- [ ] Security fix
- [x] Documentation update
- [x] Tests
- [ ] Refactor
- [ ] New skill

## Changes Made

- Added `agent/model_policy.py` for policy context building, first-match rule selection, runtime application, and structured results.
- Wired policy evaluation into `agent/conversation_loop.py` before session creation and before any LLM call.
- Added policy runtime state initialization in `agent/agent_init.py`.
- Added default-off `model_policy` config and validation warnings in `hermes_cli/config.py`.
- Added user docs at `website/docs/user-guide/features/model-policy.md`.
- Added the new docs page to `website/scripts/generate-llms-txt.py`.
- Added focused tests in `tests/agent/test_model_policy.py`.
- Added design notes in `docs/plans/2026-05-25-model-policy-routing.md`.

## How to Test

1. Configure a session-scoped policy in `~/.hermes/config.yaml`:
   ```yaml
   model_policy:
     enabled: true
     rules:
       - name: email_private
         scope: session
         on_unavailable: error
         when:
           platform: email
         use:
           provider: lmstudio
           model: local-private-model
   ```
2. Send an email gateway turn without configuring `lmstudio`; Hermes should return a policy error before any LLM call.
3. Configure the provider and retry; Hermes should select the policy target and keep it for the session.
4. Configure a `scope: turn` rule with an unavailable provider; Hermes should skip it and continue with the default route.
5. Run targeted tests:
   ```bash
   scripts/run_tests.sh tests/agent/test_model_policy.py -q
   ```

## Testing Performed

- Syntax check passed:
  ```bash
  python -m py_compile agent/model_policy.py agent/conversation_loop.py agent/agent_init.py hermes_cli/config.py tests/agent/test_model_policy.py website/scripts/generate-llms-txt.py
  ```
- `scripts/run_tests.sh tests/agent/test_model_policy.py -q` could not run in this checkout because no `.venv` or `venv` exists.

## Checklist

### Code

- [x] Changes are scoped to model policy routing.
- [x] Tests added for new behavior.
- [x] Default behavior is unchanged because `model_policy.enabled` defaults to false.
- [x] No prompt/toolset/memory mutation is introduced.
- [ ] Full test wrapper run completed.

### Documentation & Housekeeping

- [x] User docs updated.
- [x] Design/change notes added under `docs/plans/`.
- [x] Config defaults and validation updated.
- [x] Cross-platform impact considered: policy matching uses Python stdlib only and existing provider resolution paths.

## Screenshots / Logs

N/A
