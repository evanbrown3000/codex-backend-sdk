# TaskFlow phase controller

`scripts/cognilode-taskflow-phase-controller` advances a `.plan` step through research, ChatGPT sandbox, and external effect jobs in the existing D1 prompt queue. It runs once per invocation. The existing recurrence can invoke it again; it does not own a clock or a second queue.

Example:

```sh
scripts/cognilode-taskflow-phase-controller \
  --plan /home/evan/Projects/codex-backend-sdk/plans/critical-path-autonomy.plan \
  --role 'Elliot Mercer' \
  --external-employee 'Rina Hale' \
  --seed-step CP-4-6
```

The D1 API must support `get_job` by exact ID, `prompt_authority=taskflow_plan`, and the `codex.research` and `codex.external-effect` providers. Each phase enqueue includes the source `.plan` path, full plan text, exact SHA-256, step, phase, and assigned employee. A changed plan produces new phase IDs; a source change detected before phase materialization stops that run.

Each step's `owner:` selects its named Codex researcher. `config/employee-slack-roles.json` records the Slack role and source message that the controller gives to that employee; a missing role stops materialization. The `--role` argument is a fallback for plans without per-step owners. The external-effect employee is named separately with `--external-employee`.

Secretary dispatch is constrained to one installed `modified_codex` candidate through its documented `--candidates-json` interface. The controller records the invocation result under the project's `.taskflow-state/` launchpad by default and only accepts a research ZIP or external-effect receipt after a verified Codex route. The research ZIP and returned ChatGPT ZIP must be physically readable by the corresponding actuator. The chat-mode queue worker continues to own authenticated sends, collection, and recovery.

For a local installed Codex run, keep `--state-root` inside the project repository so its workspace sandbox can write the research ZIP. A plan step must declare an absolute, read-only `effect_probe_command` and exact `effect_probe_expected` output. The controller executes this probe independently after the external employee returns and again during reconciliation. A claimed effect receipt without a passing probe remains pending.

An `effect_pending` job is reconciled from its durable invocation and artifact/effect receipt. An uncertain external mutation is not sent again automatically. The external-effect receipt must describe a concrete applied effect and successful observed checks; a ZIP or source commit alone cannot complete the phase. Only completed `codex.external-effect` jobs with external-effect evidence release dependent `.plan` steps.

## Bounded recurrence service

`deploy/systemd/user/cognilode-taskflow-phase-controller.service` keeps a long-running, single-instance recurrence host active so CP-4-6 can progress without a human repeatedly invoking the controller. The service does **not** claim or time `chatgpt.com` jobs; it only reruns the phase reconciler/materializer. The hosted historical rhythm remains the sole ChatGPT clock.

Install `scripts/cognilode-taskflow-phase-controller-service` beside the controller, copy `taskflow-phase-controller.env.example` to `%h/.config/cognilode/taskflow-phase-controller.env`, and reconcile the named employees with the installed organization before enabling the unit. Research and external-effect employee names must be distinct. The recurrence process has a nonblocking singleton lock, a 10–3600 second bounded interval, a 30–7200 second per-run timeout, durable JSONL receipts, and no automatic replay of an `effect_pending` external mutation.

For this plan the acceptance probe is exactly:

```sh
/usr/bin/systemctl --user is-active cognilode-taskflow-phase-controller.service
```

An `active` service is necessary but not sufficient for CP-4-6 completion: a real `codex.external-effect` D1 job must also complete with deployment/service-change identity and central readback.
