# Test data strategy: archiving the pilot client, expanding the synthetic company set

Status: proposal, 2026-08-31. Companion to
[`white_cube_demo_plan.md`](white_cube_demo_plan.md) (the synthetic company that
exists) and [`semantic_layer_connectivity_design.md`](semantic_layer_connectivity_design.md)
(the `dbt` provider that makes an offline archive queryable).

## Bottom line

1. Archive the pilot client in **two tiers**: snapshots (cheap, freezes today's answers)
   and the **relations themselves at row grain in DuckDB** (the tier that keeps
   paying). Snapshots alone cannot answer a question you have not already asked.
2. Eight relations back the current trees. Take those, plus the four `dim_`
   tables, the attribution marts, and the user-grain intermediates. Take the whole
   column set and the whole history. Do not clean it.
3. Prove the archive works **before** access ends, by diffing offline RCAs against
   the snapshots taken from the live warehouse.
4. Write down what actually happened in the business at each visible incident.
   That is the part that is genuinely unrecoverable.
5. Yes to more synthetic companies, retail first and sales-led B2B second, but the
   work is a second **entity generator**, not a second config. Fix the two
   ground-truth bugs in `fake_companies` before building on it.
6. More synthetic companies widen the ground-truth axis and do nothing for the
   surprise axis. Keep at least one real dataset in rotation, always.

---

# Part 1: the pilot-client archive

## 1. What is actually being archived

Not "a dataset". The value is the exceptions, and they are already on the board:

- **`active_subscription_count` bound to an event-grained relation.** Two entities
  out of ~2,340 retained between adjacent windows. That single observation is why
  `entity_flows` reports `retention_share` and raises a caveat below 5% instead of
  confidently labelling every entity "new" (`engine/slices.py`).
- **A governed metric and a hand-written one disagreeing by 57%.** `paid_cmau`
  came back 18,581 from the warehouse SQL and 7,952 through the bridge, because the
  governed definition filters `feature_area = 'select'` and the hand-written SQL
  never did. That is the whole argument for the bridge's *total resolution or skip*
  filter invariant, discovered rather than reasoned about.
- **A metric whose meaning changed under it.** `trial_starts` collapses to near-zero
  before March 2026 because the free-tier business model was retired, not because
  anything is broken. Validity windows exist because of this.
- **A pre-aggregated bridge with no customer dimension.** `fct_mrr_movements` is one
  row per (day, currency). Every "slice MRR by anything about the customer" question
  is unanswerable against it, which is a real constraint no synthetic generator would
  have thought to impose.

None of those were invented. A synthetic company can only contain exceptions someone
thought of first, which is why this archive matters and why it should not be the last
real dataset in rotation.

## 2. Two tiers, and why tier 1 is not enough

**Tier 1: snapshots.** `.breakdown/snapshots/*.parquet`, one file per
(metric, grain, kind, window) and per (metric, dimension, grain, kind, window).
White Cube's 60 files total 536 KB, so cost is not a consideration.

What tier 1 freezes: the metric set, the dimension set, the grain set. What it
cannot do:

- a dimension not already declared on the tree
- a grain the tree does not already use
- a metric outside the tree
- **entity flows at all** (`snapshots.py` delegates `fetch_entity_flows` and never
  caches it, because flows are keyed by a *pair* of windows)
- anything that exercises SQL generation, provenance, filter resolution, or the
  bridge's manifest translation

Tier 1 is a regression harness. It tells you Breakdown's answers have not drifted.
It cannot find a new exception, which is the thing this dataset is for.

**Tier 2: the relations, at row grain, in DuckDB, plus the dbt project.** Everything
except freshness. This is the one to spend effort on.

Do both. Tier 1 costs an afternoon and is what you diff against.

## 3. The tables

The 25 metrics in `subscription_net_new_mrr_tree.yml` and the 8 in
`subscription_net_new_mrr_tree_dbt.yml` resolve to **eight relations**:

| Relation | Grain | Backs | Note |
|---|---|---|---|
| `fct_mrr_movements` | (day, currency) | 12 metrics: the whole MRR waterfall, `new_subscriber_count`, voluntary/involuntary churn counts | Tiny. Already aggregated, no customer dimension. |
| `fct_mrr_movement_events` | one row per movement, customer grain | `avg_new_customer_mrr_usd`, `avg_voluntary_churn_mrr_usd`, `avg_involuntary_churn_mrr_usd` | The only customer-level MRR table. This is what makes `entity_flows` testable. Take all of it. |
| `fct_mrr_daily` | (subscription_id, day) | `active_customer_count` | Subscription-day grain, so the largest revenue table. |
| `fct_new_customer_paths` | one row per new customer | `new_customers_from_trial`, `..._from_lapsed_trial`, `..._without_trial` | Carries `trial_path`, `has_signup_record`. |
| `fct_signups` | one row per signup | `total_signups` | Carries `is_qualified`, `qualification_basis`, `volume`. |
| `fct_select_trials` | one row per user (`trial` entity is `user_id`) | `trials_started`, `trial_conversions`, `trial_activation_rate`, `trial_paid_conversion_rate` | Entity is the user, not the trial. Worth preserving exactly. |
| `fct_user_activity_28d` | (user_id, feature_area, end_day_inclusive) | `paid_cmau` | Rolling 28-day windows, so by far the biggest table. Size this before extracting. |
| `fct_signup_cohort_milestones` | one row per signup cohort member | `trial_starts` (dbt-bridge variant) | Carries the seven `has_*` / `converted_*` flags. |

**Also take, though the current trees do not reference them:**

- `dim_users`, `dim_accounts`, `dim_subscriptions`, `dim_plans`. Every dimension the
  current tree slices by happens to live on its own relation, which means the archive
  as-scoped cannot exercise **dimension resolution through an entity join** at all.
  That is the `user__country`-reached-through-a-foreign-entity path, and it is
  exactly the case White Cube documents as unavailable on its MRR marts. Without the
  dims you lose the ability to ever test it against real data.
- `fct_signup_attribution`, `fct_signup_acquisition`, `fct_signup_channels`,
  `fct_affiliate_web_traffic`. Channel and attribution dimensions, plus the
  acquisition branch already documented as unusable before 2026-04-13. Keep the
  "metric changed meaning mid-history" case reproducible.
- `fct_paid_user_retention`, `fct_customer_tier_transitions`,
  `fct_select_activations`, `fct_edit_activations`. Adjacent marts that make a
  *different* tree over the same business possible, which is the cheapest way to get
  a second real test case out of one archive.
- The user-grain intermediates: `int_select_trials`, `int_windows_28_days`,
  `int_select_active_user_days`, `int_signup_events_with_qualification`,
  `int_web_touches_classified`. These are the grain below the marts. If the
  user-level-metric-construction question ever gets picked up (the open question on
  whether user-grain metrics enable a causal engine), this is the only layer that can
  answer it, and it does not exist anywhere else.
- The `seeds/` directory: `plans.yml`, `channel_taxonomy.yml`,
  `signup_source_groups.yml`. Small, and the marts will not build without them.

**Skip:** `models/base/**` raw sources (Stripe, ml_uploads, core events). Largest,
most identifying, least reusable. Rebuilding marts from raw is not a workflow worth
preserving.

## 4. How to take it

- **Full history.** No date filter. Fit length is a live design question ("encourage
  the longest feasible fit period"), and a truncated archive answers it once, wrongly.
- **Row grain.** No pre-aggregation. Aggregating to week destroys grain testing and
  kills `entity_flows` outright.
- **Every column**, not only the ones the tree uses. Columns are free; a second trip
  to the warehouse is not.
- **Do not clean it.** Keep the NULLs, the late-arriving rows, the odd categories, the
  long tail, the weeks where the pipeline broke. The mess is the asset. A cleaned
  archive is a synthetic dataset with worse provenance.

### De-identification that preserves the test value

The governing invariant: **every transformation must preserve the identities in the
tree.** `net_new = new + expansion + contraction + churn + reactivation` has to still
hold to the cent, or every formula node fails to reconcile and every test result is a
false positive.

- **IDs:** hash with one salt, applied consistently across every table. Referential
  integrity and cardinality survive exactly.
- **High-cardinality dimensions:** do not bucket. The long tail and the `__null__`
  share are precisely what the slicing code is being tested on.
- **Identifying categoricals:** map 1:1 to stable pseudonyms, preserving the frequency
  distribution.
- **Dates:** do not shift. If a shift is required, apply **one global constant offset**
  to every date column in every table, or the 30-day and 14-day lags stop lining up.
- **Money:** keep it. If it must be obscured, apply **one secret multiplicative
  constant** to every monetary column in every table. Never per-row jitter, which
  breaks the arithmetic identities.
- **Drop:** free text, emails, names, IP addresses, raw user agents, survey
  free-response. Keep parsed derivatives (device, country, classified channel).

## 5. The offline harness

The `dbt` provider is what makes this work without rewriting anything. It reads
`target/semantic_manifest.json` from plain `dbt parse` (dbt Core, no Cloud, no
Fusion) and generates its own SQL, and `duckdb` is a supported dialect and connector
(`dbt_provider._connect_duckdb`, `dbt_sql` dialect map).

1. Clone the dbt repo, source and seeds, not just `target/`.
2. Extract the relations above to parquet.
3. Load into a DuckDB file with schema names matching the project's.
4. Add a `duckdb` output to `profiles.yml` and run `dbt parse --target duckdb`.
   `node_relation` re-resolves against the local tables, so no manifest editing.
5. Point both trees at it, run `breakdown doctor`, re-run the RCAs, and **diff
   against the tier-1 snapshots taken from the live warehouse**.

Step 5 is the acceptance test and the reason to do all of this *before* access ends.
Any difference is the extraction, not the engine, and you can still go back and fix it.

## 6. The part that expires fastest

For a real dataset there is no `meta.ground_truth`. What exists instead is people who
remember what happened, and that access ends with the engagement.

Before it does, write down for each incident visible in the tree: the window, what
actually happened in the business, how it was confirmed, and what the tree ought to
conclude. Three or four of these turn the archive from "real data" into "real data
with an oracle", which is what makes White Cube valuable and what no amount of
re-querying can reconstruct later.

## 7. Permission and repo hygiene

- Get it in writing, scoped to **derived artifacts** as well as the raw extract.
  Snapshots and committed test fixtures are derived works.
- Breakdown is heading to PyPI and open source. Client-derived fixtures never live
  in the public repo. Private fixture repo, private CI job; the public suite runs on
  synthetic data only.
- Worth making "de-identified data archive at engagement end" a **standing clause in
  the Polyculture engagement agreement**, so this is negotiated at signature rather
  than asked as a favour in the last week of every project.

---

# Part 2: more synthetic companies

## 8. Decide what each company is for

Demo companies want to be **recognizable**. Test companies want to be **weird**.
Those pull in opposite directions, and a company built to be both usually makes a
mediocre demo. Declare the job in the config header. Some overlap is fine; ambiguity
is not.

## 9. What White Cube does not cover

White Cube is: subscription revenue, self-serve, high volume, small units, continuous
daily operation, an MRR stock-and-flow apex, one short fixed lag. In priority order,
the structural gaps:

**1. Retail / ecommerce, transactional.** No subscription stock at all. Revenue is
orders x AOV. New behaviour it forces:
- heavy annual seasonality against a finite fit window, which is where BSTS is most
  likely to be wrong and most likely to look right
- promotions as *known interventions*, so what-if has a checkable answer
- returns as a negative flow landing weeks after the sale: a long, **distributed**
  lag rather than the fixed 7-day one, which the declared-lag machinery has never
  been tested against
- inventory as a constraint that makes some what-ifs physically impossible, not merely
  unprecedented (a second use for `share: true`-style declared bounds)

It is also the vertical that currently cannot be pitched.

**2. B2B SaaS, sales-led.** Low counts, lumpy, one deal moves the topline.
- small-N, where priors carry the fit and Gaussian assumptions are worst
- long and *variable* sales-cycle lags
- account concentration: one logo at 20% of ARR breaks the many-small-contributors
  assumption that makes slice output readable
- `knowledge/b2b_mrr_tree.yml` already exists as a tree with no data under it, so this
  is the cheapest of the two to reach a first result on

**3. A non-continuous or non-money apex.** Northern Nights is literally this
(once-a-year event timing) and is a real client, so it is probably better acquired as
a second *real* dataset than built synthetically.

Usage-based / consumption pricing and two-sided marketplaces are the next tier. Both
change tree topology enough to be worth doing eventually, neither before the two above.

## 10. The generator is the work, not the configs

`fake_companies`' entity layer is funnel-and-subscription shaped: `signup_rate`,
`trial_start_rate`, `trial_convert`, `churn`. A retail company is not a config change,
it is a second entity generator. `white_cube_demo_plan.md` already documents the shape
of that cost: adding `app_version` was an eight-file change across four registries.

The split that survives contact:

- **Shared substrate:** the latent daily driver panel with anomaly injection, the
  observation layer (loading lag, DQ faults), and the ground-truth recorder. These are
  genuinely business-model independent and they are where the cleverness lives.
- **Per business model:** the entity layer. The entity layer *is* the business model.

Do not try to make one config schema express retail and SaaS and marketplaces. That
produces a configuration language nobody can write, including its author six months on.

## 11. Fix two bugs before building on it

Both recorded in `white_cube_demo_plan.md`, both **ground-truth correctness** bugs:

- Segment-scoped anomalies are honoured only for `signup_rate.<channel>`.
  `entities/funnel.py` is the only code reading the `|`-keyed segmented entries;
  `churn.*`, `trial_convert`, `trial_start_rate` and engagement all call plain
  `panel.get(...)`, so a `segment:` on those is **silently ignored** and lands on the
  topline.
- `_segment_matches` skips segment dimensions absent from the frame, so
  `segment: {plan: ...}` on a signup driver matches everything, with no error.

A test oracle that silently records the wrong ground truth is worse than no oracle: it
makes Breakdown look wrong when it is right, and right when it is wrong. Three more
companies inherit both. Fix them first, and add a post-generation check that every
declared anomaly measurably moved the series it claims to have moved.

## 12. Definition of done for a new company

A dataset is not the deliverable. A company counts when it has:

1. a config, regenerable from one command
2. at least three scripted stories with recorded ground truth
3. a metric tree
4. a verify script asserting Breakdown recovers each story, running in CI

That is exactly what White Cube has (`verify_white_cube_stories.py`) and it is the only
reason it is worth anything. A synthetic company without an asserted oracle is just
more data.

## 13. Cost control

Each company is a Fly instance, a dbt project, a snapshot set to rebuild, CI minutes,
and a start date that goes stale. Four of those is real drag on a one-person shop.

- Only demo-facing companies get deployed instances. Test-only companies are fixtures
  in the repo.
- One `make refresh-<company>` that regenerates data, rebuilds snapshots and redeploys,
  so staleness is one command rather than a project.
- Consider the on-demand path for pitch-specific flavour (generate a rough twin of the
  prospect the morning of the pitch) and keep only two or three companies *maintained
  and verified*. An unverified generated company is a demo prop; a verified one is a
  test asset. Do not confuse the budgets.

## 14. The blind spot worth naming

Synthetic data contains only the exceptions someone thought of. That is the whole
reason the pilot client's dataset earned its keep, and the reason adding companies 3 and 4
does not substitute for it.

The portfolio is two axes, not one:

|  | Ground truth known | Ground truth unknown |
|---|---|---|
| **Synthetic** | White Cube, and companies 3 and 4 | (nothing, by construction) |
| **Real** | incidents with a written oracle | the pilot client at large, Northern Nights |

Adding synthetic companies widens the top-left cell only. Keep at least one real
dataset live at all times, and treat the de-identified archive as a standing
end-of-engagement deliverable rather than a one-off favour.
