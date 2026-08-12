# Filter support on bindings — design spec

Status: designed, not built. Roadmap item **2.17**. Extends
[`semantic_layer_connectivity_design.md`](semantic_layer_connectivity_design.md)
— §4 is the binding contract this adds a field to, §4.1 is the scope boundary
that field has to get past, and §6 is the grain claim it changes the meaning of.
Follows [C15](roadmap.md#horizon-0--correctness-numbers-the-engine-cant-defend),
which built the parsing half and the refusal this replaces.

One decision here revises standing guidance and is stated before anything else,
in §2: **a `bind:` field may be populated by import without being authorable by
hand.** That distinction does not exist today, every field on `BindingSpec` is
both, and inventing it is how filter support gets past §4.1 rather than around
it.

## 1. Product framing

A dbt metric narrows its measure with `filter:` more often than by any other
means — regional revenue, paid signups, orders excluding test accounts,
subscriptions excluding internal tenants. The 2026-08-12 review put filters at
**~80% of real trees**. It is the ordinary case, not a corner.

Until 2026-08-12 breakdown translated those metrics *filterless*: `_Node` sets
`extra: "ignore"`, the manifest models didn't declare `filter`, and a filtered
metric became a binding over the whole relation, served under the governed
metric's name, several times too large, with `doctor` green because the grain
assertion sees one row per grain key either way. C15 fixed that the only way it
could be fixed at the time — by refusing the metric by name, since `BindingSpec`
has nowhere to put a predicate.

So the position today is honest and narrow: **the dbt path serves a project with
no filtered metrics.** That is a demo, not a client. C15 didn't cause the
capability gap, it made it visible — which is the right order, and is why this is
a follow-on item rather than something C15 should have waited for.

What C15 *did* build is the half that is hard to get right and easy to get
silently wrong. `WhereFilter` / `WhereFilterIntersection` normalise all four
serialisations MetricFlow has ever accepted, at all four locations dbt writes
them, with two traps pinned by tests (`fill_nulls_with: 0` is falsy; an empty
intersection is a truthy model). None of that has to be redone.

### 1.1 What is actually left

The roadmap row says the missing piece is "`BindingSpec` having anywhere to put
one". That understates it, and the understatement is worth correcting up front
because it is the difference between a small item and a medium one.

**dbt does not write SQL into `where_sql_template`. It writes Jinja.**

```
{{ Dimension('order__is_food_order') }} = true
{{ Dimension('customer__country') }} IN ('US', 'CA')
{{ TimeDimension('metric_time', 'week') }} >= '2024-01-01'
{{ Metric('revenue', group_by=['customer']) }} > 1000
```

`order__` is an **entity link**, not a table alias. MetricFlow resolves it
through the semantic graph into either a column on the measure's own relation or
a join, and which one it is cannot be read off the string. A filter reference is
therefore a *resolution problem against the semantic manifest*, and only after it
resolves is there any SQL to compile. That is the work.

## 2. Where the §4.1 line moves, and why it still holds

§4.1 amended the "no metric definition language" non-goal once, and set a test
every proposed field must pass:

> Does this field describe **how to fetch one series for one node**, or does it
> describe **shared org-wide semantics**? The second is out.

plus a stop rule: **`sql:` answers every feature request first**, and a field
lands only if `sql:` genuinely cannot express the thing *and* the concept is
required for correctness — "never for convenience".

A `where:` passes the first test easily. A predicate is not reusable, not named,
not referenced by another node, and carries no governance; it describes which
rows of *this* relation feed *this* series. It is as fetch-shaped as
`time_column`.

It fails the stop rule outright — for a hand author. `bind.sql` already
expresses every filter anyone could write:

```yaml
food_revenue:
  bind:
    sql: SELECT * FROM analytics.fct_orders WHERE is_food_order
    grain_key: order_id
    ...
```

That is not a workaround. It is the escape hatch working exactly as designed: the
author owns the SQL, the warehouse owns the dialect, and `doctor` still asserts
the grain over it. A hand-writable `where:` would buy a shorter spelling of
something already expressible, which is the definition of convenience.

But the *importer* has no escape hatch. It cannot write `bind.sql`, because
composing a `SELECT` around a manifest predicate means rendering the Jinja
anyway — and having rendered it, wrapping it in a subquery instead of putting it
on the binding is strictly worse: `doctor` can no longer see the predicate
separately from the relation, provenance shows a synthesised query instead of a
compiled one, and every diagnostic in `dbt_sql` loses the ability to run
with-and-without it.

**So the line moves from *which fields exist* to *which fields an author may
write*.** `where` is the first field on the import-only side of it.

The new boundary has teeth only if it is stated as a rule rather than as an
exception, so:

> **An import-only field must be fully derivable from the source artifact, with
> no information from the author.** The moment a field needs the author to
> supply anything — a hint, an override, a disambiguation — it is an authoring
> field and faces §4.1's stop rule in full.

`where` qualifies: everything in it comes from `where_sql_template` plus the
semantic model it was written against. Nothing is asked of the author. If v2
cross-join support (§12) turns out to need a declared join path from the author,
that is not an extension of this field — it is a new authoring field, and it goes
back through §4.1.

## 3. Decisions

Four decisions, each with the alternative that was live. They are recorded
because each has a plausible opposite and re-litigating them silently would be
worse than changing them deliberately.

### 3.1 `where:` is import-only; the hand-authored answer stays `bind.sql`

**Decision.** `BindingSpec.where` is populated by `dbt_bridge` and by nothing
else. A `where:` key inside a hand-written `bind:` block is a **parse error**
naming `bind.sql` as the answer.

The discriminator is already structural and needs no cleverness: manifest
bindings live in `BridgeResult.bindings` and never pass through YAML;
hand-authored bindings arrive as `MetricDefinition.bind`. The check is one
validator on `MetricDefinition`.

**Why not author-writable raw SQL.** It is dialect-specific, unportable between
warehouses, invisible to the grain claim as a separate thing, and — decisively —
adds nothing `sql:` doesn't already do. This is the roadmap row's one claim this
spec rejects; the row says "authorable directly for hand-written ones", and §2 is
why not.

**Why not a structured predicate DSL** (`where: [{dimension: region, op: in,
values: [US, CA]}]`). Portable and safely composable, and it is the beginning of
an expression language: the first `NOT`, the first `OR`, the first `IS NULL`, the
first date arithmetic each arrive as a feature request with no principled place
to stop. It also cannot represent what dbt actually writes, so the importer would
need the raw path regardless and we would own two.

**Cost, stated plainly.** A client whose filter lives in a hand-written binding
writes six more characters of SQL and loses the separate row-count check of §8.2.
That is the whole cost, and it is the right one to pay to keep §4.1 intact.

### 3.2 A filter reference resolves to a column on the binding's own relation, or the metric is skipped

**Decision.** v1 resolves `{{ Dimension('<entity>__<name>') }}` **only** when
`<entity>` is the primary entity of the binding's own semantic model and
`<name>` is a *categorical* dimension on that model. Anything else — a foreign
entity link, an unknown name, a time dimension, `TimeDimension`, `Entity`,
`Metric`, or a `Dimension` call with a grain argument — leaves the metric in
`skipped`, with the specific reference quoted.

**Why not reuse `bind.dimensions`.** The roadmap row proposes resolving filter
references "to the same join paths `bind.dimensions` already declares". The code
does not support that premise: `_categorical_dimensions` emits **same-relation
dimensions only**, deliberately —

> "A dimension reached through an entity join is a many-to-one hop the generator
> could make, but proving it many-to-one needs the joined model's primary entity,
> so it is deferred rather than assumed."

— so there are no manifest-derived join paths to reuse. Join dimensions exist
only in hand-authored bindings, and §4.2 says a hand-authored binding shadowing
an importable metric is a `doctor` error. Reusing them would mean import fidelity
depended on hand annotation, which inverts §4.2's whole policy.

**Why not require the author to declare the dimensions a filter needs.** Same
inversion, plus it makes an imported metric silently unimportable until someone
notices a `doctor` line — and by §2's rule, a field needing author input is an
authoring field.

**Why not resolve joins from the manifest now.** This is the tempting one, and
it is refused for a reason sharper than "unproven join":

> **A filter is more dangerous across a join than a slice is.** A slice that
> fans out is *visible* — the slices stop summing to the total and the engine
> already reports that. A filter that fans out is invisible: it multiplies the
> total, and the grain claim cannot see it because the grain claim counts the
> fact relation, not the join result.

Cross-join filters are §12's first follow-on and are blocked on manifest-derived
join dimensions being *proven* many-to-one, which is its own item.

**Why categorical only, excluding time dimensions.** `{{ Dimension('order__ordered_at') }}`
looks like a plain column comparison, but MetricFlow renders time dimensions at a
declared granularity, and whether it truncates here is exactly the kind of
question that can only be *answered*, not reasoned about, by [2.14](roadmap.md#horizon-2--make-it-repeatable-a-stranger-can-onboard).
Refusing costs a small set of metrics; guessing costs a wrong number with a
plausible shape.

### 3.3 The predicate is compiled through sqlglot's AST, never pasted as text

**Decision.** The rendered predicate is `sqlglot.parse_one(..., read=<target
dialect>)`, qualified by walking its `exp.Column` nodes and setting the fact
alias, and emitted with `.sql(dialect=<target dialect>)`. A predicate that fails
to parse, or that contains a subquery, a set operation or a statement separator,
is refused — the metric goes to `skipped`.

**Why not string interpolation into the WHERE clause.** Two independent reasons.
Qualification is the first: `build_query` LEFT JOINs a dimension table when a
slice is requested, so an unqualified `region` in a filter is ambiguous against
`bd_dim.region` and resolves differently — or errors — depending on the
warehouse. Textual prefixing cannot fix that, for the same reason `_qualified`
already refuses to prefix anything that isn't a lone identifier.

The second is the C15-sibling precedent. A quoted `"date"` is an identifier on
DuckDB and a string literal on Spark and BigQuery; `TRUNC(col, 'DAY')` returns
**NULL** on Spark rather than erroring. The lesson written into `dbt_sql`'s
module docstring is *generate in the target dialect, never translate into it, and
treat "the transpiler accepted it" as no evidence at all.* Parsing the predicate
in the target dialect is the cheapest form of that discipline; §9 covers what it
does not catch.

### 3.4 The grain claim runs post-filter, and a second check proves the filter is live

**Decision.** `build_grain_assertion` applies the binding's `where`, so the
assertion is made over the rows the node actually aggregates. A new check,
**`filters narrow`**, separately proves against the warehouse that the compiled
predicate excludes some rows and not all of them (§8.2).

**Why not pre-filter.** Fan-out is a property of the relation, so pre-filter is
the more conservative check — and it is conservative in the wrong direction. A
`fct_order_lines` relation filtered to `line_number = 1` is one row per order
*under this binding* and multi-row without it; pre-filter would fail a binding
whose every number is correct. The grain claim exists to protect the aggregate
this node computes, and that aggregate is the filtered one.

**What post-filter loses, and what covers it.** A post-filter pass no longer
tells the author their relation is unsafe if they ever widen the filter, and it
cannot catch a mis-translated predicate (a predicate that wrongly drops rows
still leaves one row per grain key). Neither is left uncovered: the first is a
line in the check's output naming the predicate the assertion was made under, so
a pass reads as *"one row per grain over these filtered rows"* rather than
*"checked"*; the second is precisely what §8.2 exists for.

## 4. The contract

```yaml
# Manifest-derived only. `bind: {where: ...}` in a tree is a parse error.
revenue_food:
  bind:
    relation: analytics.fct_orders
    grain_key: order_id
    time_column: ordered_at
    agg: sum
    measure: order_total
    where:
      - "is_food_order = TRUE"
```

| Field | Notes |
|---|---|
| `where` | `List[str]`, ANDed. Each entry is one resolved `WhereFilter`, over columns of this binding's own relation, unqualified. Empty list and absent mean the same thing: no filter |

Four properties, each load-bearing:

- **A list, not a joined string.** dbt's `WhereFilterIntersection` is an AND of
  separate predicates and `.predicates` already returns them that way. Keeping
  the structure means a skip reason, a `doctor` line and a *show query* panel can
  quote the one predicate that matters instead of a concatenation.
- **Unqualified columns.** The fact alias (`bd_fact`) is a `dbt_sql` concept and
  must not leak into the binding — the same binding is compiled by five different
  builders with different alias sets. Qualification happens at build time (§3.3).
- **Resolved, not templated.** `where` holds SQL over real columns; the Jinja is
  gone by the time it is stored. A binding is a fetch descriptor, and a template
  that still needs the semantic graph to mean anything is not one.
- **Dialect-agnostic as written, dialect-parsed as used.** The text is stored as
  dbt wrote it (modulo reference substitution) and re-parsed in the target dialect
  by every builder, exactly as `bind.sql` and every measure expression already are.

### 4.1 Two things `where` must not silently break

**C16's `definition_sha` must cover it.** The snapshot key fingerprints the
provider-side definition; if it does not serialise `where`, editing a dbt
metric's filter serves the pre-edit numbers forever *and* `query_provenance`
attests the new predicate beside them — C16's exact failure, reintroduced by a
new field. This needs a test, not a reading of the code.

**A node-level `bind:` override replaces the manifest binding entirely**
(`dbt_provider`), so an override on a filtered metric drops the filter. That is
already true of every other field and §4.2's shadowing error is the answer, but
it is worth one line in the override's `doctor` message once filters exist,
because dropping a filter is the one override that quietly changes the number
rather than the plumbing.

## 5. Resolution: template → predicate

Per metric, given the semantic model `M` the binding resolved to and its primary
entity `P`:

1. Take `filter.predicates` — non-empty templates only; the empty intersection is
   not a filter (C15 already establishes this and it must not be re-derived).
2. Scan each template for `{{ ... }}` calls. Every call must be
   `Dimension('<ref>')` with exactly one string argument; anything else refuses
   the metric by name, quoting the call.
3. Resolve `<ref>`:
   - `"<entity>__<name>"` where `<entity> == P.name` and `<name>` is a
     `categorical` dimension on `M` → substitute `_column(dimension)`.
   - `"<name>"` with no entity link, naming a categorical dimension on `M` →
     substitute. (MetricFlow normally writes the link; accepting the bare form
     costs nothing and is unambiguous within one model.)
   - `<entity>` naming a **non-primary** entity on `M` → refuse: *"references
     `<ref>` through entity `<entity>`, which is a join to another semantic model;
     cross-relation filters are not compiled yet."*
   - anything else → refuse: *"references `<ref>`, which is not a categorical
     dimension on semantic model `<M>`."*
4. Parse the substituted text with sqlglot in the target dialect. A parse failure,
   or a subquery / set operation / statement separator anywhere in the tree,
   refuses the metric.
5. Store the surviving predicates on `BindingSpec.where`.

**The invariant, and it is the whole safety argument:** a metric is translated
only when *every* predicate of *every* filter it carries resolves. There is no
partial translation, no "best effort", no dropped conjunct. Anything short of
total resolution leaves the metric in `skipped` exactly where C15 left it — the
first slice of this work is therefore strictly a superset of today's behaviour,
and its failure mode is refusal, never a wrong number.

The four locations C15 checks stay checked. Metric-level `filter` and the measure
input's `filter` both resolve against `M` and are ANDed together. `join_to_timespine`
and `fill_nulls_with` are untouched and still refuse. Per-input filters on a
ratio's numerator/denominator and on a derived metric's inputs still refuse, and
for a reason this spec does not weaken: those become formula edges over metrics
*by name*, and a name carries no scope. Giving one side of a ratio its own
predicate is a modelling change, not a SQL change (§12).

## 6. Compilation: one clause, every builder

`dbt_sql` gains one function and calls it from everywhere:

```python
def _where_predicates(bind, read, alias) -> list[Expression]:
    """Parse, validate and alias-qualify `bind.where` for one query."""
```

Applied in:

| Builder | Why it must be there |
|---|---|
| `build_query` | the series itself, sliced or not |
| `build_resolved_slice_query` | entity-grain resolution must collapse the *filtered* rows, or its slices sum to the wrong total |
| `build_entity_flow_query` | new/resurrected/churned are defined against the filtered population; a user leaving the filter's scope is a churn |
| `build_multivalue_assertion` | asserts multi-valuedness over rows the node never reads, otherwise |
| `build_grain_assertion` | §3.4 |

The last three all route through `_bounded(query, bind, read, start, end)`, which
already takes the binding — extending it to apply the predicate alongside the
window bounds covers them in one place and makes "every diagnostic sees the same
rows as the series" structural rather than remembered.

**The invariant to test, not to assume:** the same predicate set is applied by
every builder, identically. A filter applied to the total query but not to the
sliced one produces slices that do not sum, which the slicing maths reads as an
unexplained residual — a wrong *finding*, not a wrong number, and harder to spot.

## 7. Slicing: do the slices still sum?

**Yes, and the reason is worth writing down rather than assuming.**

Slice attribution requires that the slices partition the rows behind the total.
A `WHERE` predicate is a row-level test evaluated independently of the `GROUP BY`,
so filtering and grouping commute: the filtered row set is partitioned by the
slice dimension exactly as the unfiltered one was. Both queries evaluate the same
predicate over the same relation, so `Σ_g filtered_slice_g == filtered_total`
holds exactly, for the same reason it holds today.

Three consequences that are not defects but will be reported as bugs if not
written down:

- **NULL is not "not matching".** `region <> 'EU'` excludes rows with a NULL
  region, because SQL three-valued logic says so — and dbt's own query does the
  same, so we agree with the governed metric. What does *not* hold is
  `filtered + complement == unfiltered`; nothing in the engine claims it, and
  nobody should add a check that assumes it.
- **A filter on the slice dimension degenerates the partition.** `where: region =
  'US'` sliced by `region` yields one non-empty slice. Correct, useless, and the
  `top_k` + other-bucket logic will render it as a single bar. Worth a `doctor`
  note eventually; not worth a refusal.
- **Non-additive aggregations are unaffected.** `count_distinct` slices don't sum
  today, for reasons that have nothing to do with filters
  ([`non_additive_slicing_design.md`](non_additive_slicing_design.md)), and
  filtering neither helps nor worsens it. The entity-grain resolution path just
  has to see the same rows (§6).

## 8. `doctor`

The `dbt` chain is currently: manifest → profile → connection → tree metrics
bind → declared dimensions exist → grain claims hold → entity grain resolves.
Filters change one step and add one.

### 8.1 `grain claims hold` — same assertion, narrower rows

Unchanged in mechanism; §3.4 in meaning. The pass message must name what it
checked over:

```
✓ grain claims hold    12 relation(s) one row per grain, 3 under a filter
                       (checked 2026-07-01 → 2026-07-31)
```

The window caveat `_over()` already exists and already says the right thing —
"absence over seven days is not proof of absence" — and it applies with full
force here, because a filter shrinks the sample the check runs on.

### 8.2 `filters narrow` — new, and the honest half of the confidence story

For every binding with a `where`, one query over the probe window:

```sql
SELECT COUNT(*)                                       AS "rows",
       SUM(CASE WHEN <predicate> THEN 1 ELSE 0 END)   AS "kept"
FROM <relation> AS bd_fact
WHERE <window bounds>
```

| Result | Verdict | What it means |
|---|---|---|
| `0 < kept < rows` | **pass**, reporting `kept/rows` | the predicate is a live predicate on this warehouse, in this dialect, against these columns |
| `kept == 0` | **fail** | the node would serve an empty or all-zero series. The signature of a dialect-hostile predicate — `= TRUE` against a `VARCHAR` column, a date literal parsed as an identifier, a boolean column stored as `'Y'` |
| `kept == rows` | **warn** | the predicate excluded nothing. Either genuinely vacuous over a short probe window, or it evaluated constant-true, which is C15's original defect arriving through a new door |
| query errors | **fail**, with the predicate quoted | an unresolvable column, a type mismatch the warehouse *does* refuse |

This is deliberately shaped like the grain claim, and for the same reason it was
a differentiator there: **it checks the data instead of trusting the metadata.**
MetricFlow does not do this either. It converts the entire class of
silently-no-op and silently-everything-drops predicates from a wrong number into
a startup failure, which is the class C15 punished.

What it does **not** do is prove our predicate means what dbt's means. `kept/rows
= 0.31` says the filter is doing something; it does not say it is doing the right
thing. §10.

## 9. Dialects

Every predicate reaching the warehouse has been parsed and re-emitted in the
target dialect (§3.3), which is the same discipline that closed the Spark `trunc`
and quoted-`"date"` bugs. That handles quoting, identifier casing, boolean
literals and function-name differences that sqlglot models.

It does not handle the class those two bugs actually belonged to: **a construct
the dialect accepts and evaluates differently.** Spark's `trunc` did not error;
it returned NULL. sqlglot transpiled it happily. Local tests could not see it.
The precedent is explicit that generation-time checking is not evidence, so
filters get the same answer that worked there:

1. **§8.2 runs against the real warehouse**, and both of that bug's signatures —
   everything collapses, nothing is excluded — are exactly what it fails on.
2. **The fixture project in CI carries filtered metrics** across every mapped
   dialect's generated SQL, executed on DuckDB and *compiled* (not executed) for
   the rest, which is the standing rule that 2.10's BigQuery extension restated:
   **generation and execution ship separately, and a mapped dialect is not a
   supported warehouse.**
3. **A filtered metric on an unmapped adapter** (generic SQL — Redshift, Trino,
   Athena, ClickHouse today) already warns; with a predicate in play the warning
   should name the filter, because generic emission is where a dialect-hostile
   predicate is most likely to survive to runtime.

## 10. Confidence without 2.14

[2.14](roadmap.md#horizon-2--make-it-repeatable-a-stranger-can-onboard) —
differential verification against MetricFlow — is the mechanism that would
*prove* our filtered SQL agrees with dbt's, and it is unbuilt and blocked on `mf`
not importing on Python 3.14. Shipping filters means shipping SQL we generate
ourselves against a definition we translated ourselves, which is the posture C15
existed to punish. So the confidence available has to be stated exactly, not
assumed.

**What is proven without 2.14:**

- The predicate resolved *totally* — every reference, every conjunct — or the
  metric was refused (§5). There is no partial translation to be wrong about.
- Every reference resolved to a column on the measure's own relation, so there is
  no join, no fan-out surface, and no join-cardinality assumption to be wrong
  about.
- The predicate parses in the target dialect and contains no subquery or set
  operation (§3.3).
- It excludes some rows and not all rows, **on the client's own warehouse**
  (§8.2).
- The relation is one row per grain key *under the filter* (§3.4).
- The predicate is visible: it is in the generated SQL, which
  [2.11](roadmap.md#horizon-2--make-it-repeatable-a-stranger-can-onboard)'s *show
  query* already displays beside the number.

**What is not proven, and must be said in the release note rather than
discovered:** that the row set we keep is the row set MetricFlow keeps. The
residual risk is concentrated in one place — dbt's rendering of a `Dimension()`
reference — and v1 shrinks it about as far as it goes by admitting only
same-relation categorical dimensions, where the rendering is a bare column and
there is very little for MetricFlow to do differently. Time dimensions, where it
demonstrably *does* do something (granularity), are refused for exactly this
reason (§3.2).

That is a real answer, and it is not the same as verification. **2.14 remains the
thing that closes it**, and this item raises its priority rather than lowering
it: before filters, 2.14 verified metrics we had barely transformed; after, it
verifies a translation step of our own.

## 11. What `BridgeResult.skipped` says now

`skipped` keeps its meaning **exactly**: metrics that were not translated and
cannot be served. It never holds a metric that was translated with part of its
filter — §5's invariant is what makes that guarantee available, and it is worth
keeping the field's meaning boring.

Two things change:

- **Filter refusal reasons get specific.** Today's blanket reason — *"declares a
  `filter` (…), which narrows it to a subset of the relation; neither a breakdown
  binding nor a formula edge can carry a filter"* — becomes **false** the moment
  any filter translates, and a reason that is false is worse than one that is
  vague. It is replaced by reasons naming the blocking construct and the author's
  next move: a cross-relation reference, an unresolvable name, a time dimension,
  a `Metric()` call, a per-input filter. `_filter_reason` stops being one string.
- **Translated filters are reported, not silent.** A filtered node is a node
  whose number is deliberately smaller than the metric a reader may have in a
  dashboard, so the fact must appear where a reader looks: `doctor`'s bind step
  gains a count (*"14 metric(s) resolved, 3 carry a filter"*), and the binding's
  `where` is available on the metric payload for the node card. No new
  `BridgeResult` field is needed — the predicate is on the binding, which is
  already the single source of truth for everything else about the fetch.

## 12. Implementation order

Each step lands green. Steps 1–3 change no number for anyone: until step 4, every
filtered metric is still skipped.

1. **`BindingSpec.where`** + the `MetricDefinition` rejection of a hand-authored
   `where:` (§3.1, §4). Parser tests only.
2. **`dbt_sql`**: `_where_predicates`, threaded through `_bounded` and the five
   builders (§6). Tested against hand-constructed bindings — the bridge does not
   emit one yet — including the alias-qualification case under a dimension join
   and the Σ-slices-equals-total case.
3. **`doctor`**: `filters narrow` (§8.2) and the grain-claim message (§8.1).
4. **`dbt_bridge`**: the resolver (§5), replacing `_unsupported_semantics`'
   blanket filter refusal with a resolve-or-skip path, and specific reasons
   (§11). This is the step where behaviour changes, and it changes from *skip* to
   *serve* only for the metrics that fully resolve.
5. **Reporting**: bind-step count, `where` on the metric payload, README and
   `docs/ai-context/python-backend.md` (the working agreement is that docs travel
   with the API surface).
6. **Roadmap and 2.10's coverage line.** 2.10 already carries the correction that
   its "+ filters" claim "was never true"; that sentence needs amending rather
   than deleting, to *"simple + ratio + derived-without-offset, plus filters
   resolving to the measure's own relation"*.

## 13. Out of scope, and what this splits into

The first three are the follow-on items. Each is refused today with a named
reason, which is the state this spec is careful to preserve.

- **Cross-relation filter references** — `{{ Dimension('customer__country') }}`
  on an orders measure. Blocked on manifest-derived join dimensions being
  *proven* many-to-one, which the bridge deliberately does not do
  (`_categorical_dimensions`). That proof is a prerequisite item in its own
  right, and §3.2 argues a filter across a join is a strictly worse thing to get
  wrong than a slice across one. **This is the largest remaining share of real
  filters and should be filed as its own row.**
- **Per-input filters on `ratio` and `derived`** — `signups(US) / sessions`.
  Formula edges reference metrics by name and a name carries no scope, so this is
  a change to how the DAG expresses a scoped parent, not to SQL generation. A
  modelling item, and the one place where "our own metric definition language"
  genuinely is the question §4.1 is guarding.
- **`TimeDimension`, `metric_time`, and grain arguments** — needs the time spine,
  the same blocker as `join_to_timespine` and derived offset windows, and belongs
  with them.
- **Author-writable `where:`** — §2. `bind.sql` is the answer, and this stays
  closed until someone is blocked in a way `sql:` cannot unblock.
- **`Metric()` filters** — a filter whose predicate is itself a metric is a
  correlated subquery over the semantic graph. Refused by name, permanently as
  far as this spec is concerned.
- **A UI badge for filtered nodes.** The predicate is visible through *show
  query* from day one, which is the disclosure that matters; a badge is a small
  follow-on, not a gate.

## 14. Open questions

- **How much of the ~80% does the local-only slice actually cover?** The review
  measured *"filters appear in ~80% of real trees"*, not *"~80% of filters
  reference only local dimensions"*. This is cheap to measure before
  implementation and would change the sequencing if it came back low: count, over
  a real manifest, filtered metrics whose every `Dimension()` reference resolves
  to the measure's own model. Do this first — it is the one number that says
  whether v1 is a capability or a rounding error.
- **Should `kept == rows` fail rather than warn?** A constant-true predicate is
  C15's defect wearing a new hat, and warning about it is exactly the "green
  `doctor` beside a wrong number" pattern this codebase keeps deciding against.
  The argument for warning is that a genuinely vacuous filter over a seven-day
  probe window is real and failing it would block a correct tree. A longer probe
  window for this one check might dissolve the question.
- **Does a filtered node need its own name in the UI?** `revenue` filtered to the
  US is not `revenue`, and a reader comparing against a dashboard has no way to
  know unless they open *show query*. The dbt metric's own name is usually
  honest (`us_revenue`), but not always, and breakdown has no opinion today.
- **Does `where` belong in the snapshot filename, or only in `definition_sha`?**
  §4.1 requires the sha covers it. Whether two differently-filtered versions of
  one metric should coexist in a snapshot directory — rather than invalidating
  each other — is a question about how snapshots are used during iterative tree
  authoring, and C16 answered it for edits, not for coexistence.
