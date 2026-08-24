# Concrete Agent Model Selection

## Goal

CARLO must keep model providers and model selection separate.

- A model provider is a connection and model catalog.
- An agent profile selects one concrete catalog model.
- A provider has no default model.

The UI identifies a selection with stable provider and external model identifiers,
for example `openai-api — gpt-5.6-sol`.

## Data model

Remove `model_providers.default_model_id` and
`agent_profiles.model_provider_id`. Keep
`agent_profiles.available_model_id` as the only managed model selection for an
agent profile. Task model overrides continue to use `tasks.available_model_id`.

The migration preserves existing configuration by copying a provider-only
profile's former default model into `agent_profiles.available_model_id` before
dropping the obsolete columns. A profile whose provider had no default remains
unconfigured. Existing concrete profile and task selections are unchanged.

## Provider catalog

Provider refresh discovers models and updates their availability and discovered
limits. It never selects a model. Provider API responses and update contracts no
longer expose `default_model_id`.

The Models settings page is limited to:

- provider connection and credential management;
- catalog refresh;
- discovered model status;
- context-window and maximum-output overrides.

## Agent profiles

Every managed CARLO session resolves a concrete `AvailableModel`. An agent
profile without `available_model_id` is visibly unconfigured and cannot start a
managed session.

The Coding agents page offers only selectable concrete models. Options are
grouped by provider and labeled with the provider slug and external model ID:

```text
openai-api — gpt-5.6-sol
omlx — Qwen3.8-27B-oQ8e-fp16-mtp
```

Saving a profile writes only `available_model_id`. Brief, plan, implementation,
discovery, escalation, review, and future profiles all use the same contract.

## Runtime and evidence

Profile resolution loads the selected model and its provider, validates
availability and effective limits, decrypts the optional provider credential,
and creates the existing isolated Pi runtime snapshot.

Planning, task, and discovery evidence continues to store the readable provider
slug and external model ID. No Pi-owned static model setting is consulted.

## Errors and compatibility

- Starting an unconfigured profile fails with a concise managed-model error.
- An unavailable model or a model without effective limits remains unselectable.
- Deleting a provider or model remains blocked while referenced.
- Existing concrete task and discovery model pins remain valid.
- Provider-only profile configuration is accepted only during migration and is
  not exposed after upgrade.

## Verification

Tests must cover:

- migration of a provider-default profile to its concrete model;
- migration of a provider without a default to an unconfigured profile;
- refresh without implicit selection;
- API contracts without provider defaults or provider-only profile selection;
- concrete profile resolution and unconfigured-profile failure;
- grouped Coding agents labels using `provider-slug — external-model-id`;
- Models settings without any default-selection control;
- preservation of task and Discovery concrete model pins;
- full backend, frontend, extension, migration, and production build checks.
