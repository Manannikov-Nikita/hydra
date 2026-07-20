# Hydra historical acceptance fixture provenance

These fixtures are a privacy-reduced projection of the local Codex rollout files used by the accepted paired audit. They contain no prompts, assistant messages, tool inputs, tool outputs, repository paths, user names, credentials, or original thread/turn/call identifiers.

The projection adds `event_msg.payload.type = hydra_fixture_activity_boundary`
to each non-root session. This fixture-only marker preserves the exact timestamp
of the last observed source envelope so summed agent spans remain independently
reconstructable. It is not a Codex event schema claim and carries no content,
token vector, tool data, or lifecycle meaning. A production adapter treats the
unknown subtype as a diagnostic while its safe envelope timestamp may contribute
to normalized activity timing.

## Source integrity

The three source files were read-only and had these SHA-256 digests at extraction time:

- accepted comparative-audit rollout: `c48950484e56937eee2adb3287d12a1ce99e281405b6e9401a58fe2b57bdb26d`
- newer task root rollout: `eabf3b7bdc385e9940d70af6aba5c38a3af9b0a3805194364119e04665b10190`
- older task root rollout: `4d93f6270fb316a27b484b70a200034ab0d8fc5f9436ad2a165e0b25f8b0febe`

Original identifiers are replaced by deterministic, synthetic UUID-shaped identifiers. `__PROJECT_ROOT__` is a materialization placeholder for tests.

The two manifests intentionally contain only dataset-level goldens and aggregate
fixture counts. Per-session vectors and timestamps are not an oracle; the test
reconstructs them from the JSONL observations.

## Accepted reconstruction rule

1. Derive the logical source thread from the UUID suffix of the rollout filename and select the `session_meta` whose `payload.id` matches it. This prevents replayed parent `session_meta` records inside child rollouts from changing the child identity.
2. Build the descendant closure from `parent_thread_id`; include only sessions created no later than the root task-completion cutoff.
3. For every included session select the last complete cumulative token event at or before the cutoff.
4. Root baseline is zero. Child baseline is the last cumulative token event at or before the logical session creation time plus one second. If no such event is observable, retain zero only as the historical audit fallback and label it `zero_no_observation`.
5. Subtract baseline from final component by component. Calculate `working_tokens = input_tokens - cached_input_tokens + output_tokens` and `full_context = input_tokens + output_tokens`. Reasoning is retained separately and is not added to output again.
6. Sum session activity spans, ending at the fixture-only activity boundary,
   separately from root wall-clock. No model annotations existed in the
   historical data, so semantic coverage is exactly zero.

The root JSONL in each fixture also contains the first cumulative token event after the task-completion cutoff. It must be excluded from the acceptance total and prevents a test from accidentally passing by simply selecting the latest event in the file.

## Independently reproduced results

| Metric | Newer | Older |
| --- | ---: | ---: |
| Sessions | 25 | 28 |
| Input | 170,932,899 | 212,729,495 |
| Cached input | 166,306,816 | 206,700,800 |
| Output | 392,273 | 559,147 |
| Reasoning, subset of output | 120,065 | 190,343 |
| Working tokens | 5,018,356 | 6,587,842 |
| Full context | 171,325,172 | 213,288,642 |
| Root wall-clock, seconds | 10,793.582 | 16,069.767 |
| Summed agent span, seconds | 37,038.540 | 66,772.237 |
| Observed child replay baselines | 4 | 1 |
| Replayed non-source session metadata records | 3 | 1 |
| Child zero fallback due to no observed replay | 20 | 26 |
| Semantic annotations | 0 | 0 |

Rounded to whole seconds, wall-clock is `2:59:54` versus `4:27:50`, and summed agent span is `10:17:19` versus `18:32:52`, matching the accepted audit.

## Confidence and limitations

- Token cumulative events, timestamps, selected tree edges, and task-completion cutoffs are exact observations from the source journals. Totals are derived from them.
- Only five child sessions expose a replay event inside the one-second fork window. A zero fallback for all other children reproduces the accepted historical audit but is not evidence that their replay baseline was truly zero. Hydra should surface that provenance instead of calling those baselines exact.
- Four child files contain a later `session_meta` for a replayed parent/ancestor. Those records are retained with synthetic identifiers so the adapter must keep filename/source identity instead of switching the observation stream to the replayed session.
- Session creation has both envelope and payload timestamps, usually separated by less than one second. The fixture retains both and uses the payload timestamp as the logical creation time. Using the envelope timestamp selects the same five observed baselines and produces the same token totals; payload timestamps reproduce the reported rounded wall-clock values.
- Summed agent span is an observable elapsed-span metric, not CPU time. Parallel sessions overlap by design.
- The fixture preserves only the session metadata, selected baseline/final
  cumulative events, root task-completion event, fixture-only activity
  boundaries, and one post-cutoff sentinel. It is not a verbatim archive and
  cannot support content, file-read, command, or semantic-phase claims.

## Verification

Run:

```bash
PYTHONPATH=src python3.12 -m unittest tests.test_historical_acceptance -v
```

The acceptance test materializes the project placeholder, imports through the
public read-only rollout adapter, then independently reduces only normalized
JSONL observations. It asserts both expected totals and zero semantic coverage.
