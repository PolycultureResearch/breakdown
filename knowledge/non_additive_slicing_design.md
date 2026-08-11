# Design: Non-additive metrics at entity grain (roadmap 3.8)

Status: draft (design only, nothing built). Companion to
[`dimensional_slicing_design.md`](dimensional_slicing_design.md) (the slice
attribution this extends — read it first),
[`semantic_layer_connectivity_design.md`](semantic_layer_connectivity_design.md)
§7–8 (where the binding contract's `entity_key` comes from) and
[`grain_design.md`](grain_design.md) (why a non-additive series may never be
resampled).

## 1. Product framing

Slicing a `count_distinct` metric does not decompose it, and the engine
currently says so in a way that reads as a data problem.

Measured on a real warehouse while building the `dbt` provider:
`active_subscription_count` sliced by `subscription_status` over two weeks came
to **2,106 against an unsliced 2,069**. Nothing is broken. A subscription that
changes status inside a day is counted once in the total and once in each status
it held, so the slices overstate by exactly the overlap — 37 subscriptions.

What the engine does with that today is the defect. `_reconciliation`
(`engine/slices.py:139`) compares `Σ_g` slices against the served series, flags
anything above 0.5% of baseline as **`discrepant`**, and the UI paints it red:

> Slices do not sum back to the metric … — reported, not rescaled.

That is the right *posture* — never silently rescale — attached to the wrong
*diagnosis*. `dimensional_slicing_design.md` §7 lists the causes it anticipates:
"a non-additive dimension, a sliced query diverging from the governed metric
definition, or freshness skew". The last two are defects a user must go and fix.
The first is a mathematical property of the metric, known in advance from its
binding, and unfixable by definition. Lumping them together tells a user their
pipeline may be broken when the truth is *this metric does not decompose this
way, and here is the one that does*.

Two things follow, and they are separable:

- **A metric whose non-additivity is known should be labelled, not flagged.**
  The binding already knows: `BindingSpec.is_non_additive` exists (§5).
- **There is a decomposition that works**, and it is more useful than the one
  being approximated: resolve the metric to entity grain, where it becomes a
  sum. That is §4.

## 2. Why the slices do not sum

Let `E_t` be the set of entities the metric counts in period `t`, so
`v[t] = |E_t|`. Slicing by dimension `d` assigns each row a value of `d`, and
the sliced query returns `v_g[t] = |{e ∈ E_t : (e, t) has d = g}|`.

`Σ_g v_g[t] = v[t]` holds **iff every entity has exactly one value of `d` in
period `t`**. It fails precisely when an entity is multi-valued in a period: a
subscription that was `active` in the morning and `cancelled` by the evening, a
user on both `ios` and `web` the same day.

So non-additivity is not a property of `count_distinct` alone — it is a property
of the **(metric, dimension) pair**. The same metric sliced by a genuinely
single-valued attribute (signup cohort, home region) sums exactly. This matters
for the design: the fix belongs on the slice, not on the metric, and a metric
that is additive under one dimension must not be penalised under another.

## 3. Three tiers of capability

What breakdown can offer depends on what the author has declared. Stating the
tiers explicitly keeps the failure mode "we can't do this, here is why" rather
than a silently degraded number.

| Tier | Declared | What breakdown gives |
|---|---|---|
| **1** | `entity_key` **and** an entity-grain relation | Exact slice attribution (§4) plus entity flows (§6) |
| **2** | `entity_key` only | Per-slice Δ for ranking, overlap named and quantified, **no contribution shares** |
| **3** | Neither | Trend only. Contribution percentages refused |

Tier 2 is roughly what ships today, minus the mislabelling. Tier 3 is the
current behaviour for a metric with no binding at all. Tier 1 is the new work.

## 4. Tier 1: resolve to entity grain, where the metric is a sum

Given a relation at **entity × period** grain, breakdown can build a
single-valued assignment and the metric becomes exactly additive.

The author declares how a multi-valued entity resolves:

```yaml
active_subscriptions:
  bind:
    relation: analytics.fct_subscription_days
    grain_key: subscription_day_id
    time_column: day
    agg: count_distinct
    measure: subscription_id
    entity_key: subscription_id
    entity_grain:
      relation: analytics.fct_subscription_days   # optional; defaults to `relation`
      resolve: last            # last | first | error
    dimensions:
      status: {column: subscription_status}
```

`resolve` is **required** when a dimension is multi-valued, and there is no
default. Picking one silently is exactly the "approximate rather than refuse"
move this project rejects: `first` and `last` answer different business
questions (*what state did they arrive in* vs *what state did they end in*), and
which is right is not ours to guess. `error` is the honest third option for an
author who believes their data is already single-valued and wants that enforced.

`doctor` checks it, in the same place it asserts the grain claim: if a
dimension is multi-valued for any entity in the probe window and `resolve` is
absent, the binding fails at startup with the offending dimension and an example
entity named. Discovering this at first *slice by* click would repeat C12.

With resolution applied, `Σ_g v_g[t] = v[t]` holds exactly, the existing
`_sum_attribution` path runs unchanged, and reconciliation returns a zero
residual for a real reason rather than a tolerated one.

## 5. Labelling, which is most of the value and nearly free

Independent of tier 1, and worth shipping first:

- The slice response gains `additivity: "exact" | "overlapping" | "unknown"`,
  taken from the binding (`is_non_additive`) and the resolution status — not
  inferred from the residual, which cannot distinguish overlap from a broken
  query.
- When `additivity == "overlapping"`, the residual is reported as **`overlap`**
  with its own field, not as `reconciliation.status = "discrepant"`, and the UI
  drops the error styling. The sentence becomes *"these slices share entities;
  they overstate the total by N (x%), which is deduplication overlap, not an
  unexplained cause"*.
- `reconciliation.status = "discrepant"` keeps its current meaning — an
  unexplained divergence a user should investigate — and therefore becomes
  trustworthy, which it is not while it fires on arithmetic.
- Contribution **shares of the gap** are withheld at tier 2. A share whose
  denominator does not reconcile is not a share. Per-slice Δ still ranks.

## 6. Entity flows: what the change was made of

Tier 1's second capability, and the one that produces findings rather than
numbers.

Comparing the reference window `R` to the analysis window `A`, classify each
entity by where it was in each:

| Class | In `R` | In `A` | Effect on slice `g` |
|---|---|---|---|
| new | — | `g` | `+1` to `g` |
| churned | `g` | — | `−1` from `g` |
| retained | `g` | `g` | none |
| migrated | `g₁` | `g₂` | `−1` from `g₁`, `+1` to `g₂` |

**Migration nets to zero across slices** — `Σ_g (in_g − out_g) = 0` — exactly as
the rate case's `mix_total` does. That symmetry is not a coincidence: both are
reallocation terms, and both are reported as their own line rather than folded
into any slice's contribution.

**The motivating finding.** A user switches platform: iOS shows `−1`, web shows
`+1`, the total is unchanged. Naive slice attribution reports two large
offsetting causes for a change that never happened. Entity flows label it
*migration* — and migration is frequently the real answer, not noise to be
explained away.

⚠️ **The exactness question, stated rather than hidden.** §4's slice
attribution is exact against the metric's window-mean gap. Entity flows as
defined here compare **window-level sets**, and `mean_t |E_t|` is not
`|∪_t E_t|` unless presence is stable within each window. So the two do *not*
decompose the same number, and this design does **not** claim they do.

Flows are therefore specified as a **diagnostic reported alongside** the exact
attribution — answering "what kind of movement produced this" — never as a
competing decomposition of the same gap. Presenting them as a second
decomposition would put two numbers on screen that do not add up to each other,
which is the failure this whole document is about. Whether an exact
window-mean-level flow decomposition exists (a stock-and-flow formulation over
per-period entries and exits) is left open in §9.

## 7. Query shape

One extra query per (metric, dimension) at tier 1, alongside the existing sliced
fetch. Sketch, dialect-generated like everything else in `dbt_sql.py`:

```sql
WITH resolved AS (            -- one row per (entity, period): the `resolve` rule
  SELECT entity, period, slice FROM (
    SELECT <entity_key> AS entity,
           DATE_TRUNC(<grain>, <time_column>) AS period,
           <dimension> AS slice,
           ROW_NUMBER() OVER (PARTITION BY <entity_key>,
                              DATE_TRUNC(<grain>, <time_column>)
                              ORDER BY <time_column> DESC) AS rn   -- `last`
    FROM <relation> WHERE <window>
  ) WHERE rn = 1
)
SELECT period, slice, COUNT(*) AS value FROM resolved GROUP BY 1, 2
```

That single query serves §4 directly: its output is the existing
`[date, slice, value]` contract, now guaranteed to sum. Entity flows (§6) need a
second query comparing the two windows' resolved sets, which is a `FULL OUTER
JOIN` on entity between the `R` and `A` aggregates.

Both are ordinary `GROUP BY`s over the binding's own relation. No new join
planning, and the many-to-one rule of `semantic_layer_connectivity_design.md` §6
is untouched.

## 8. Uncertainty

Slice attribution's intervals come from the circular moving-block bootstrap over
per-period series, resampled jointly across slices (`slices.py`). Tier 1 changes
nothing there: the resolved per-period series is an ordinary per-period series,
so the existing bootstrap applies unchanged and each slice keeps its excess
interval and `prob_concentrated`.

Entity flows are **counts over the two windows, not per-period series**, so the
block bootstrap does not apply to them as written. v1 reports flow counts as
point values with no interval, and says so — a bare count labelled as such is
honest; a fabricated interval is not. Giving flows an interval means resampling
entities, which is a different resampling unit and belongs in its own item.

## 9. Explicitly deferred

- **An exact window-mean flow decomposition** (§6). If a stock-and-flow
  formulation reconciles entity flows to the window-mean gap exactly, flows
  graduate from diagnostic to decomposition. Until then they stay a diagnostic.
- **Intervals on entity flows** (§8) — needs entity-level resampling.
- **Semi-additive stocks** (`agg: last` with an `entity_key`). The same
  entity-grain machinery answers balances and headcount, and
  `semantic_layer_connectivity_design.md` §5.2 already refuses `agg: last` in
  the generator. Sequencing it after `count_distinct` keeps one problem in
  flight at a time.
- **Multi-dimension entity flows** (migration across two dimensions at once).
  Cross-product slicing is deferred in `dimensional_slicing_design.md` §9 for
  the same reason.
- **Automatic `resolve` selection.** Permanently. The choice between `first` and
  `last` is a business question, and guessing it is the error class this
  document exists to remove.
