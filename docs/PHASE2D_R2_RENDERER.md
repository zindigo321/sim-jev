# R2 renderer compatibility

Renderer and scoring identities are independent. The supported complete combinations are:

| Output protocol | Renderer | Template | Interpretation |
| --- | --- | --- | --- |
| `local-ranking-v1` | `lossless-tables-and-shared-json-v1` | `public-portfolio-ranking-v2` | Historical strict complete permutation |
| `local-preference-scores-v2` | `lossless-tables-and-shared-json-scores-v2` | `public-portfolio-scores-v2-a` or `-b` | Historical R1 strict integer scores |
| `local-preference-scores-v2` | `lossless-canonical-tables-and-shared-json-v3` | The same registered score templates | R2 canonical input rendering, unchanged strict integer scores |

`validate_identity` validates the complete shape and the registered renderer, output
protocol, template hashes, schema, sort rule, request/arithmetic protocols and model
runtime identity. `render_for_identity` validates before dispatching. Unknown versions
and mixed combinations fail. The v1 and historical R1 rendering algorithms remain
available, including their insertion-order-sensitive reference allocation. R2 never
retags those responses or rewrites their keys, prompts, tokens or experiment freezes.

New code explicitly opts in with `identity_v3(manifest, **identity_v2_arguments)`.
Alternatively, an offline contract check may deep-copy a validated historical score
identity and replace only `renderer` with `CANONICAL_RENDERER`, then call
`validate_identity`. This creates a new identity; it does not create model evidence.
Existing `identity_v2` calls continue to choose the historical renderer.

The new renderer visits every JSON object in ascending Unicode key order before
table construction, repeated-subtree counting and reference assignment. It preserves
array order, exact integer/float JSON representations, complete fields and all public
values, including the supplied candidate-array order. Short IDs are assigned
independently by original stable candidate ID, without reordering the candidate
array in the canonical input or prompt. Historical v1/v2 retain their existing pool
sorting. The input encoding is explicitly named
`canonical-object-order-and-literals-v3`; `$literal` escapes literal objects whose
shape would collide with `$ref`, `$columns`/`$rows`, or `$literal` markers. The prompt
documents decoding. `unfactor` retains the historical path for inputs without the
new encoding marker and rejects unknown encoding versions.

Scoring remains exactly one integer from 0 through 100 per supplied short ID, with
no extra keys. Booleans, floating scores, non-finite values, duplicate/missing keys
and additional fields are rejected. Equal scores are ordered by original stable
candidate ID. Scores remain uncalibrated preferences.

Cache keys bind the full identity and complete rendered record. Record validation
also compares their canonical content hashes, so JSON integer/float type changes
cannot pass through Python's permissive numeric equality. Each attempt's output
schema and decoding options also require exact canonical content; raw, stream,
truncate and shift require their declared boolean values. The renderer change
therefore isolates caches. Cache-only misses fail as `local_cache_miss` without any
model or metadata attempt; there is no cross-version fallback. A historical record
placed under a new key is rejected. Tests use explicit offline fixtures and never
claim them as new real model generations. New renderer GPU inference and effects
remain unvalidated in this engineering-only round.

Targeted verification:

```powershell
& 'D:\conda_envs\sim-jev\python.exe' -m unittest tests.test_phase2d_r2_renderer -v
```

The tests exercise ordinary and sorted JSON, nested object-key permutations,
fresh-process recovery, exact prompt/mapping/schema/cache identity, reserved-marker
round trips, numeric types and precision, business-array order, cache isolation,
raw-score/ranking/prompt/schema corruption and strict version dispatch. Historical
balanced-40-168 prompts remain bound to the two pre-fix hashes in the counterexample
test. Offline token-budget receipts are separate deliverables and do not establish
live validity.
