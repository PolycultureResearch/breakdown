# How the White Cube tree got written

The demo claim is that an agent can author a Breakdown tree inside an existing
dbt repo. This is the record of that actually happening for
[`white_cube_tree.yml`](white_cube_tree.yml) — including the parts that went
wrong, because those are the parts that show what the loop is really like.

Nothing here is reconstructed for the demo. It is what the work was.

## The loop

**1. Read what the semantic layer already offers.** Not the YAML — the compiled
manifest, through MetricFlow:

```bash
mf list metrics
mf list dimensions --metrics signups
```

This matters more than it sounds. Dimension identifiers are
`<primary_entity>__<dimension>` (`mrr_movement__plan`, `trial__country`,
`user__device`) and are **not** guessable from the semantic model YAML — the
entity prefix depends on which entity is primary, and joined dimensions appear
under the entity they arrive through. Every `dimensions:` block in the tree was
pasted from that command's output.

It also surfaced something the semantic model YAML did not: `new_mrr` and
`churned_mrr` can be grouped by `user__country` even though `fct_mrr_movements`
has no country column, because MetricFlow resolves it through the `user` foreign
entity into `dim_users`. Reading the model files alone would have concluded that
MRR was not sliceable by geography.

**2. Write the tree top-down from the identity that has to hold.** Start at the
apex and work down, because the exact-attribution edges are the ones worth
getting right:

```yaml
- name: net_new_mrr
  formula: "new_mrr + expansion_mrr - contraction_mrr - churned_mrr"
  parents: [new_mrr, expansion_mrr, contraction_mrr, churned_mrr]
```

Two derived metrics had to be added to the dbt project to make the layer below
that exact — `new_arpu = new_mrr / nullif(new_subscriptions, 0)` and its churn
twin. That is the normal direction of travel: the tree tells you which metric
the semantic layer is missing, and you go add it there rather than working
around it in the tree.

**3. Let `doctor` check the wiring, not your reading of it.**

```bash
breakdown doctor --tree demo/white_cube_tree.yml --start-date … --end-date …
```

It resolves every `${VAR}`, finds `mf`, lists metrics, and reports whole-period
counts per node against the 10-period fit minimum. Faster and more honest than
re-reading the YAML.

## What went wrong, and what it taught

**A weekly rate cannot take a daily weight.** `trial_conversion_rate` is declared
at week grain; the obvious weight for slicing it is `trials_started`, which is a
daily node. The slice endpoint rejected it outright:

> Rate 'trial_conversion_rate' (grain 'week') has weight 'trials_started' at
> grain 'day'; sliced weights must share the rate's grain.

Caught by touching all 36 declared (metric, dimension) pairs once — not by
reading. The fix was to drop the declaration and record why inline, because the
question it would have answered is better answered on `trial_conversions`, a
flow, in conversions rather than percentages.

**A ratio can only be sliced by dimensions both halves share.**
`visit_signup_rate` is signups ÷ sessions, and MetricFlow offers it only
`user__*` dimensions — the ones reachable from both. But a session that never
signed up has no user, so ~96% of the weight landed in `__null__` and the slice
said nothing. Also dropped, also with the reason recorded in the tree.

Both of these are the same lesson: **a dimension being listed is not the same as
it being meaningful on that metric.** An agent will happily declare every
dimension the semantic layer advertises. Touching each one once is what separates
the ones that answer a question from the ones that produce a table of noise.

**The data has to be checked, not assumed.** The first version of the Brazil
campaign confined its lift to paid-social. Brazil is 9% of traffic and
paid-social ~18% of signups, so the segment was ~1.6% of the funnel — invisible
next to the campaign's own global spend ramp. The slice found nothing. The fix
was a config change plus
`fake_companies/scripts/verify_white_cube_stories.py`, which checks each planted
story twice: that the topline moved, and that the injected slice concentrates
hard enough to be ranked first.

## The part worth showing a prospect

Not the YAML. The YAML is ~150 lines and unremarkable — that is the point.

Show the *loop*: `mf list dimensions` → write the block → `breakdown doctor` →
run the slice → find out the dimension is degenerate → delete it and say why.
The agent is fast at the first three steps and the semantic layer is what makes
them possible. Step four is judgment, and it is where the time actually goes.
