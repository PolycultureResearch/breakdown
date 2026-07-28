# Authoring deterministic decompositions — lessons for agents

Hard-won, generalizable guidance for adding **deterministic identity edges**
(`formula` nodes — sums and products) to a metric tree, distilled from building a
`new_mrr = new_members × new_member_arpu → …` sub-tree against a warehouse
provider. The canonical field reference is [`../README.md`](../README.md); this
doc is the *how to not get it subtly wrong* companion. Most of it generalizes to
any provider; a few notes are warehouse-SQL specific.

## The one thing to get right: the identity holds **per day**, not per window

The engine runs **one Shapley game per analysis-window day**
(`engine/rca.py::shapley_attribution`): it pulls each parent's *daily* series over
the window, evaluates `formula` **element-wise on the daily arrays**, then takes
`.mean()`. So a `formula` is a **contemporaneous per-day identity**, and every
node it references must be a daily series for which the identity holds on each
individual day.

Consequence for **rate/ratio factors** in a product `A = B × C`: you cannot
define `C` as "window-numerator / window-denominator". `C` must be a **daily**
series whose value each day is that day's ratio, so that `B[day] × C[day] = A[day]`
holds day by day. Mean-of-daily-ratios ≠ ratio-of-window-sums — the node's
displayed number is the former; don't expect it to equal a hand-computed monthly
rate.

## The node-shape pattern for `A = B × C` (or `B + C`)

- **`A` (the parent / target):** carries **both** its own ground-truth `sql`
  **and** the `formula` + `parents`. Its actual/baseline come from *its own
  series* (`rca.py` reads `window_mean(frame, A)`); the `formula` is used only to
  *distribute* A's window-over-window gap across B and C. A node being both a
  formula node and a SQL-backed node is normal and expected — intermediate nodes
  are.
- **`B`, `C` (the factors):** each a leaf with its **own daily `sql`**. For a
  count factor, `SELECT day, COUNT(*)`. For a **rate factor**, its SQL returns the
  **daily ratio itself** (e.g. `average_order_value = daily_revenue / daily_orders`),
  exactly like the shipped `examples/jaffle_shop_tree.yml`.

YAML order doesn't matter — the DAG is built by name, then topologically sorted.
A node may have multiple children (fan-out is fine).

## Zero-denominator days: filter them OUT, don't emit NULL

Missing dates are reindexed to **0.0** (`data_fetch.py`: `reindex(full, fill_value=0.0)`),
but a **present** date with a NULL/NaN value stays NaN (`astype(float)`), and one
NaN poisons the whole window mean. So a rate node's SQL must **emit no row** on
days where its denominator is 0 (use `WHERE denom > 0`, or an `INNER JOIN` that
naturally drops them). Then:

- zero-volume day → rate row absent → reindexed to `0` → product `0 × 0 = 0`,
  which matches the count factor also being `0`. Identity holds.
- **Never** write `num / NULLIF(denom, 0)` and let it pass through — that emits a
  NULL on zero days and breaks the mean. The reindex-to-zero mechanism only works
  if the day is *absent*, not *NULL*.

## `formula` and `lags` are mutually exclusive

The parser rejects a node with both: a formula is a contemporaneous identity and
cannot have time-lagged parents. So a deterministic decomposition **replaces** a
probabilistic lagged edge into the same node — it doesn't augment it. If you're
turning a former BSTS leaf (e.g. `trial_starts → new_mrr` at lag 30) into a
`formula` node, you must drop the old `parents`/`lags`/`priors` on that node.
Probabilistic edges elsewhere in the tree are untouched.

## Validate before trusting attribution

The `formula` reconstruction and the target's own SQL are **two independent
measurements**; any gap is an unexplained residual in the attribution. Before
committing, check they agree over a **real window**, on the warehouse, not just
that the YAML parses:

1. **Run each node's SQL standalone** (literal dates) — catch column/dialect
   errors the parser can't see.
2. **Residual check** — compute `mean(formula(daily parents))` vs
   `mean(target's own daily series)` over a representative window; confirm they're
   close (single-digit %). This is where **cross-grain / cross-table** products
   leak: e.g. a numerator from one table and denominator from another, bucketed by
   different keys, won't reconcile exactly.
3. **Hunt the asymmetric-zero days** — days where the target is nonzero but the
   reconstruction is zero (or vice versa). `COUNT_IF(target > 0 AND reconstruction = 0)`
   should be ~0; anything material is a date-anchor or grain mismatch that will
   show up as phantom attribution.

## Date bucketing / grain is a modeling decision, not a detail

Whether an identity is *exact* depends on how each node is bucketed:

- **Sum identities are robust** *iff every addend shares the same date anchor*
  (e.g. all bucketed by `first_paid_at`). Then `parent[day] = Σ children[day]`
  trivially.
- **Products across different grains or date anchors** are only exact "by
  construction" if you define the rate as the quotient of the two — and even then
  the daily identity can leak on boundary days. A rate whose numerator and
  denominator are bucketed by **different** dates (different cohorts sharing a
  calendar day) is a **blended same-period** ratio: mechanically valid for
  Shapley, but it is *not* a cohort rate — document that so downstream readers
  don't over-interpret the number.
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
names must be a subset of `parents`, etc.) offline. The *data* still needs a real
connection — that's what the residual check above is for.
