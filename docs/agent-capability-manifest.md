# Agent Capability Manifest

This file is the lightweight entry point for agents consuming `codex-backend-sdk`.
It exists to prevent repeated rediscovery of integration surfaces.

## Discovery rule

Prefer existing SDK surfaces before creating new transports, brokers, queues, or credential paths.

## Capability map

| Capability | Entry point | Owner boundary |
| --- | --- | --- |
| Chat-mode turns | `client.chatgpt.operations` / `b4pt0r chatgpt` | provider operation layer |
| Remote execution selection | `b4pt0r remote list/select/exec` | remote relay |
| App Server compatibility | `cognilode-b4pt0r-app-server` | bridge layer |
| Conversation reuse | `b4pt0r memory` | Agent Memory facade |
| Provider continuation | broker JSON operation boundary | custody-owned broker |

## Extension rule

New integrations should add a capability entry with:

- consumer
- stable entry point
- owning layer
- required state
- reuse path

Do not document successful experiments only as prose; make them discoverable by future agents.
