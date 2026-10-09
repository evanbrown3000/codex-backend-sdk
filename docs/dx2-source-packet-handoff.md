# DX-2 research packet handoff (proposal; apply after DX-1 effect/checkoff)

The existing DecisionX TaskFlow plan has DX-1 in flight. Do not change its plan
revision or enqueue DX-2 from this proposal. After DX-1 has a terminal external
effect and verified checkoff, amend only the DX-2 step with these fields:

```plan
    required_nested_source_packet: /home/evan/.local/share/cognilode/taskflow-launchpad/decisionx/DX2_historical_S3_500_relevant_20261009.zip
    required_nested_source_sha256: 5d2ae3f433867effd71cc5aa926b80e034c906b63ecbb4e4824d0e65ab9e4f15
    required_nested_source_minimum: 500
```

Add one DX-2 `do` instruction to Nadia Brooks: verify the source ZIP and its
MANIFEST.json, then include the entire ZIP unchanged as one member of her
research ZIP. The research ZIP must list that nested member with its SHA-256 in
its own top-level MANIFEST.json. The standard Chat-mode queue physically
attaches the research ZIP only after the Codex research phase completes.

The controller change in this branch checks the complete source ZIP's SHA-256,
requires exactly one intact nested copy inside the research ZIP, verifies at
least 500 distinct manifest-listed conversations, and rehashes every nested
rendered conversation. The check runs before research completion and Chat-mode
job creation, including crash reconciliation. It does not rank, index, or
analyze conversations on EvanPC; substantive descriptor/outcome work belongs
in the ChatGPT Chat-mode agent's native sandbox. The S3 object origin still
requires provenance analysis; the packet proves transport/readback and exact
membership, not the authority of a directory name.

Before merging/deploying this controller branch, verify the recorded ZIP digest
and run the focused TaskFlow tests. Keep
the historical rhythm queue and existing D1 lease/completion path unchanged.
