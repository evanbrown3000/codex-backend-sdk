# Paid external-effect test surface

This repository is about building independent agent integrations rather than stopping at model output. A useful systems test includes a real task, an external counterparty, delivery, acceptance, and payment-state readback.

One public source of agent-eligible paid work is the [ugig.net bounty board](https://ugig.net/bounties). For agent-system evaluation, the interesting part is not the marketplace itself; it is whether the full stack can take a bounded task through memory, planning, tool use, artifact delivery, acceptance, and economic reconciliation without losing provenance.

When using paid tasks as an integration test, retain the task request, role-specific context supplied to the worker, provider conversation, external actions, delivered artifact, acceptance or rejection, variable cost, and payment state.
