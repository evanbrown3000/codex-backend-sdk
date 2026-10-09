# TaskFlow phase controller

`scripts/cognilode-taskflow-phase-controller` advances a `.plan` step through research, ChatGPT sandbox, and external effect jobs in the existing D1 prompt queue. It runs once per invocation. The existing recurrence can invoke it again; it does not own a clock or a second queue.

Example:

```sh
scripts/cognilode-taskflow-phase-controller \
  --plan /home/evan/Projects/codex-backend-sdk/plans/critical-path-autonomy.plan \
  --role 'Elliot Mercer' \
  --external-employee 'Nadia Brooks' \
  --seed-step CP-4-6
```

The D1 API must support `get_job` by exact ID, `prompt_authority=taskflow_plan`, and the `codex.research` and `codex.external-effect` providers. Each phase enqueue includes the source `.plan` path, full plan text, exact SHA-256, step, phase, and assigned employee. A changed plan produces new phase IDs; a source change detected before phase materialization stops that run.

Secretary dispatch is constrained to one installed `modified_codex` candidate through its documented `--candidates-json` interface. The controller records the invocation result under the project's `.taskflow-state/` launchpad by default and only accepts a research ZIP or external-effect receipt after a verified Codex route. The research ZIP and returned ChatGPT ZIP must be physically readable by the corresponding actuator. The chat-mode queue worker continues to own authenticated sends, collection, and recovery.

For a local installed Codex run, keep `--state-root` inside the project repository so its workspace sandbox can write the research ZIP. A plan step must declare an absolute, read-only `effect_probe_command` and exact `effect_probe_expected` output. The controller executes this probe independently after the external employee returns and again during reconciliation. A claimed effect receipt without a passing probe remains pending.

An `effect_pending` job is reconciled from its durable invocation and artifact/effect receipt. An uncertain external mutation is not sent again automatically. The external-effect receipt must describe a concrete applied effect and successful observed checks; a ZIP or source commit alone cannot complete the phase. Only completed `codex.external-effect` jobs with external-effect evidence release dependent `.plan` steps.
