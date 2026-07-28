# Authoring deterministic decompositions — lessons for agents

Hard-won, generalizable guidance for adding **deterministic identity edges**
(`formula` nodes — sums and products) to a metric tree, distilled from building a
`new_mrr = new_members × new_member_arpu → …` sub-tree against a warehouse
provider. The canonical field reference is [`../README.md`](../README.md); this
doc is the *how to not get it subtly wrong* companion. Most of it generalizes to
any provider; a few notes are warehouse-SQL specific.

## The one thing to get right: the identity holds **per period**, not per window

The engine runs **one Shapley game per window period** at the node's grain
(`engine/rca.py::shapley_attribution`): it pulls each parent's per-period series
over the window (finer flow/stock parents resampled up to the node's grain),
evaluates `formula` **element-wise on the period arrays**, then takes `.mean()`.
So a `formula` is a **per-period identity at the node's grain**, and every node
it references must yield a series for which the identity holds on each
individual period. (For an all-daily tree that means: per day.)

Consequence for **rate/ratio factors** in a product `A = B × C`: you cannot
define `C` as "window-numerator / window-denominator". `C` must be a
**per-period** series (at the node's grain) whose value each period is that
period's ratio, so that `B[t] × C[t] = A[t]` holds period by period.
Mean-of-period-ratios ≠ ratio-of-window-sums — the node's displayed number is
the former; don't expect it to equal a hand-computed monthly rate.

If the ratio is degenerate at daily grain (ARPU on a 1-member day; conversion
on a low-volume day), **don't fight it — declare the identity at a coarser
grain**: give the node and its rate factor `grain: week` (or `month`) and
`kind: rate`, and let finer flow parents auto-aggregate up. That is exactly
what per-node grain exists for (see
[`grain_design.md`](grain_design.md)).

## The node-shape pattern for `A = B × C` (or `B + C`)

- **`A` (the parent / target):** carries **both** its own ground-truth `sql`
  **and** the `formula` + `parents`. Its actual/baseline come from *its own
  series* (`rca.py` reads `window_mean(frame, A)`); the `formula` is used only to
  *distribute* A's window-over-window gap across B and C. A node being both a
  formula node and a SQL-backed node is normal and expected — intermediate nodes
  are.
- **`B`, `C` (the factors):** each a leaf with its **own per-period `sql`** at
  its declared grain (one row per period, period-start dates — weeks start
  Monday, months on the 1st). For a count factor, `SELECT day, COUNT(*)`. For
  a **rate factor**, its SQL returns the **period ratio itself** (e.g.
  `average_order_value = daily_revenue / daily_orders`), exactly like the
  shipped `examples/jaffle_shop_tree.yml`.

YAML order doesn't matter — the DAG is built by name, then topologically sorted.
A node may have multiple children (fan-out is fine).

## Zero-denominator periods: what happens depends on `kind`

The warehouse fetcher reindexes each series onto the spine of whole periods at
the metric's grain and gap-fills **by `kind`** (`data_fetch.py`): `flow` → 0,
`stock` → forward-fill, `rate` → **a missing period is a hard fetch error** (a
rate cannot be invented). A **present** period with a NULL/NaN value stays NaN
(`astype(float)`), and one NaN poisons the whole window mean. So:

- **Never** write `num / NULLIF(denom, 0)` and let a NULL pass through — a NULL
  row breaks the mean regardless of kind.
- The **best** fix for a rate with zero-denominator periods is a **coarser
  grain**: declare the rate (and the identity) at a grain where the denominator
  is never zero. Zero-volume days stop being a special case at all.
- If you keep a fine-grain rate and declare it `kind: rate` (as you should),
  gaps are errors — so on a zero-denominator period the SQL must **emit an
  explicit `0` row** (`COALESCE`, or a spine join). The identity still holds:
  count factor `0` × rate `0` = target `0`.
- A rate left at the default `kind: flow` keeps the old absent-row → 0
  behavior, but then coarsening it is silently *summed* — declare rates as
  rates.

## `formula` + `lags`: cohort-aligned lagged identities

A formula node's identity is contemporaneous by default, but `lags` turns it
into an **exact lagged identity**: `A[t] = f(each parent shifted back by its
lag, in grain steps)`. Use this when the cohort structure is real —
`conversions[t] = trial_starts[t−14] × cohort_rate[t]` — instead of settling
for a blended same-period ratio or a fully probabilistic edge. The engine
reads each lagged parent from correspondingly shifted windows in both the
Shapley attribution and the residual fit, so the identity (and its exact
attribution) holds cohort by cohort. The per-period identity rule above still
applies after the shift: `A[t]` must equal `f(shifted parents at t)` on each
individual period.

A deterministic decomposition still **replaces** a probabilistic edge into the
same node — when converting a former BSTS edge (e.g. `trial_starts → new_mrr`
at lag 30) into a `formula` node, drop the old `priors`, keep `parents`, and
keep the lag only if the identity is genuinely cohort-aligned at that lag.
Probabilistic edges elsewhere in the tree are untouched.

## Validate before trusting attribution

The `formula` reconstruction and the target's own SQL are **two independent
measurements**; any gap is an unexplained residual in the attribution. Before
committing, check they agree over a **real window**, on the warehouse, not just
that the YAML parses:

1. **Run each node's SQL standalone** (literal dates) — catch column/dialect
   errors the parser can't see.
2. **Residual check** — compute `mean(formula(per-period parents))` vs
   `mean(target's own per-period series)` over a representative window, at the
   node's grain; confirm they're close (single-digit %). After the covariance
   symmetrization (1.8), RCA's `unexplained` on the node reports exactly this
   residual — an exact identity shows ~0. This is where **cross-table**
   products leak: e.g. a numerator from one table and denominator from
   another, bucketed by different keys, won't reconcile exactly.
3. **Hunt the asymmetric-zero days** — days where the target is nonzero but the
   reconstruction is zero (or vice versa). `COUNT_IF(target > 0 AND reconstruction = 0)`
   should be ~0; anything material is a date-anchor or grain mismatch that will
   show up as phantom attribution.

## Date bucketing / grain is a modeling decision, not a detail

Whether an identity is *exact* depends on how each node is bucketed — and the
engine now gives you first-class tools for both dimensions of the choice:
**`grain`** declares the period the identity holds at (finer flow/stock
parents auto-aggregate up), and **`formula` + `lags`** declares a
cohort-aligned lagged identity (`conversions[t] = trial_starts[t−k] ×
cohort_rate[t]`). Reach for those before contorting SQL.

- **Sum identities are robust** *iff every addend shares the same date anchor*
  (e.g. all bucketed by `first_paid_at`). Then `parent[day] = Σ children[day]`
  trivially.
- **Products across different date anchors** are only exact "by construction"
  if you define the rate as the quotient of the two — and even then the
  per-period identity can leak on boundary periods. A rate whose numerator and
  denominator are bucketed by **different** dates (different cohorts sharing a
  calendar day) is a **blended same-period** ratio: mechanically valid for
  Shapley, but it is *not* a cohort rate. If the cohort structure is real,
  declare the lag instead of blending; otherwise document the blend so
  downstream readers don't over-interpret the number.
- Call out **coverage cliffs** (an event/source that only became reliable after
  some date): the identity may still hold structurally (everything piles into the
  complementary branch pre-coverage) while one branch carries no signal — say so
  in the node description and the tree header, or an agent will read a flat-zero
  branch as "no effect" rather than "no data".

## Validating a warehouse tree offline

Provider config is validated at **parse time**, so a warehouse tree fails to
construct if its connection env vars are unset. To validate *metric definitions
and the DAG* without connecting, set throwaway values:

```bash
DATABRICKS_HOST=x DATABRICKS_HTTP_PATH=x DATABRICKS_TOKEN=x \
  uv run python -c "from breakdown.parser import Parser; import networkx as nx; \
  p = Parser(open('path/to/tree.yml').read()); \
  print(p.dag.number_of_nodes(), nx.is_directed_acyclic_graph(p.dag))"
```

This runs the full Pydantic + formula-reference + DAG-acyclicity checks (formula
names must be a subset of `parents`, plus the cross-node grain rules) offline.
The *data* still needs a real connection — that's what the residual check above
is for.

## Postscript: what these lessons became

The frictions this guide documented fed roadmap items 1.7 and 1.8, both now
shipped:

- "Ratio factors are degenerate at daily grain" → **per-node `grain` + `kind`**
  ([`grain_design.md`](grain_design.md), [`grain_research.md`](grain_research.md)):
  declare the identity at its natural grain instead of forcing daily.
- "`formula` and `lags` are mutually exclusive" (this guide's original rule) →
  **relaxed into cohort-aligned lagged identities**: an exact deterministic
  form for trial→member conversion.
- "The residual check is a manual step" → the covariance symmetrization (1.8)
  made RCA's `unexplained` report exactly that residual, and the two-level
  attribution view shows the means bridge and co-movement shift separately.

Earlier revisions of this guide describing the pre-grain, pre-1.8 behavior are
in git history.
