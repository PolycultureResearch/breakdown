# Design: the White Cube demo (a deployable, synthetic-data Breakdown instance)

Status: planned. Companion to
[`dimensional_slicing_design.md`](dimensional_slicing_design.md) (the slice UI
this plan finally builds is that design's largest remaining gap),
[`grain_design.md`](grain_design.md) (the demo tree is deliberately mixed-grain),
and [`rca_lag_assessment.md`](rca_lag_assessment.md) (the 7-day trial lag is the
tree's showpiece edge).

## 1. What this is for

A linkable, always-on Breakdown instance a prospect can open and use, over a
synthetic B2C subscription SaaS with planted, ground-truth-labeled anomalies.
It exists to demonstrate five things, in this order:

1. **YAML → picture.** How little it takes to get a metric tree on screen when a
   decent dbt Semantic Layer already exists — the artifact a data team and an
   exec team can align on.
2. **An agent writes the YAML.** Claude Code, in an existing dbt repo, reading
   the semantic manifest and authoring the tree.
3. **Root-cause analysis in the UI.** Traverse the tree, then slice to localize.
4. **Root-cause analysis from Claude/ChatGPT** through the Breakdown MCP server.
5. **What-if mode.**

Non-goals: this is not hosted multi-tenant Breakdown (roadmap 3.5). It is one
read-only tree, served fast, that several people can use at once.

## 2. The company

**White Cube** — a B2C subscription app for visual artists: organize an archive
of digital images and files, track what showed in which exhibition, and log what
sold. Freemium with a **7-day trial**, plans `free` / `studio` ($12/mo) /
`professional` ($29/mo).

The data comes from [`fake_companies`](https://github.com/PolycultureResearch/fake_companies),
which simulates exactly this shape bottom-up: a latent daily driver panel →
entity-level raw rows → an observation layer with loading lag and data-quality
faults. Rate anomalies are applied at the latent layer, so they **cascade
causally** downstream rather than being painted onto an aggregate. Every
injection is recorded in `meta.ground_truth`, which is what lets us assert in CI
that the demo still works.

Timeline: `start: 2024-06-01`, `days: 790` (≈ 2026-07-30), so the demo reads as
current rather than as a historical artifact. Regenerate with a later start when
it begins to look stale — it is one config line and a rebuild.

## 3. The four stories

Each is one scripted anomaly, chosen so the whole chain is visible and the
conclusion is checkable against ground truth.

| # | Story | Driver | Chain the RCA should recover |
|---|---|---|---|
| A | A release breaks the mobile signup CTA on paid-social landing pages | `signup_rate.paid_social` × `{device: mobile}`, `level_shift 0.55` | signups ↓ → trials ↓ → **7 days later** conversions ↓ → new MRR ↓. Slice `signups` by `device` → mobile. |
| B | Professional-tier subscribers start cancelling | `churn.professional`, `level_shift 1.9`, ≥60-day window | churned MRR ↑ → net new MRR ↓. Slice `churned_mrr` by `plan` → professional. |
| C | **Positive:** a Brazil-targeted campaign lands well | `spend.paid_social` `ramp 1.35` + `signup_rate.paid_social` × `{country: BR}` `level_shift 1.5` | spend ↑ → sessions ↑ → signups ↑. Slice `signups` by `country` → BR far above baseline share. |
| D | **Positive:** an onboarding revamp lifts trial conversion | `trial_convert`, `level_shift 1.25` | trial conversion rate ↑ → conversions ↑ → new MRR ↑. Pure tree RCA, no slicing. |

Plus two or three `dq` anomalies (`volume_dropout` on `product.events`,
`null_spike` on `web.sessions`) and `surprise: {count: 2}` for ambient realism,
all held outside the demo windows via `exclude_windows` so a canned analysis is
never muddied. The DQ events are a talking point in their own right: they move
the *observed* rows without moving business truth, which is what the observation
layer is for.

### Why these stories and not the ones originally sketched

The original framing was an **app-version** regression and a **regional** churn
spike. Two generator facts pushed this to a staged approach:

- **`app_version` does not exist** in `fake_companies` — not in the config
  schema, the entity generators, the raw schemas, or dbt. Adding it is a
  ~8-file change across four separate registries, and a believable *rollout*
  needs a time axis that `mix` (a static distribution) does not have.
- **Segment-scoped anomalies are only honoured for `signup_rate.<channel>`.**
  `entities/funnel.py` is the only code that reads the `|`-keyed segmented
  entries the panel writes; `churn.*`, `trial_convert`, `trial_start_rate`, and
  engagement all call plain `panel.get(...)`, so a `segment:` on those is
  **silently ignored** and lands on the topline instead. Worse,
  `_segment_matches` skips segment dims absent from the frame, so
  `segment: {plan: …}` on a signup driver matches everything with no error.
- Separately, **`fct_mrr_movements` and `fct_subscription_days` carry no user
  geography** — only `plan` / `category` / `billing_period` — so "MRR by region"
  is not queryable without joining `dim_users` into both marts.

So Phase 1 tells all four stories with dimensions that work today (`device`,
`country`, `plan`), and §8 carries the app-version and geo-on-MRR upgrades.

### One fidelity constraint

Churn, upgrade, downgrade, and resurrection run on a **30-day bucket loop**
(`entities/lifecycle.py`, `_DAYS_PER_MONTH = 30`): the driver is sampled once per
bucket and the resulting events land on a uniformly random day inside it. A churn
anomaly therefore smears roughly ±1 week past its configured window. Story B's
window spans **two full buckets (≥60 days)** so the level shift is unambiguous,
and the guided tour's RCA windows sit in the smear-free interior. Fixing this
properly is generator work, not demo work — see §8.

## 4. The metric tree

`demo/white_cube_tree.yml`, apex `net_new_mrr`, 23 nodes — small enough to read
on a pitch screen, deep enough that RCA has to actually traverse.

```
net_new_mrr (week, flow)                                          APEX
  = new_mrr + expansion_mrr - contraction_mrr - churned_mrr       [formula]
  ├ new_mrr (week)      = new_subscriptions * new_arpu            [formula]
  │   ├ new_subscriptions (week)                                  [measured lagged identity]
  │   │   = trial_conversions[lag 1] + reactivations + direct_conversions
  │   │   ├ trial_conversions (week) = trials_started * trial_conversion_rate
  │   │   │   ├ trials_started (day, flow) ← signups              [probabilistic]
  │   │   │   │   └ signups (day) = sessions * visit_signup_rate  [formula]
  │   │   │   │       ├ sessions (day) ← marketing_spend          [probabilistic]
  │   │   │   │       │   └ marketing_spend (day, flow)
  │   │   │   │       └ visit_signup_rate (day, rate)
  │   │   │   └ trial_conversion_rate (week, rate)
  │   │   │       ← trial_activation_rate, trial_days_active      [probabilistic]
  │   │   ├ reactivations (week, flow)
  │   │   └ direct_conversions (week, flow)
  │   └ new_arpu (week, rate)
  ├ churned_mrr (week)  = churned_subscriptions * churn_arpu      [formula]
  │   └ churned_subscriptions (week) = active_subscriptions * customer_churn_rate
  │       └ customer_churn_rate (week, rate) ← member_activity_rate  [probabilistic]
  ├ expansion_mrr (week, flow)
  └ contraction_mrr (week, flow)
```

The choices below are deliberate, and each demonstrates a documented feature
rather than decorating one:

**Every way into a paid plan is a term of an exact identity.**
`new_subscriptions = trial_conversions[lag 1] + reactivations +
direct_conversions` — a *measured* cohort-aligned lagged identity: the node
keeps its own `source`, so the identity is checked against the books at load
and `unexplained` means the ledgers disagree, not "noise". The one-week lag
*is* the 7-day trial: `trial_conversions` is cohort-dated (`fct_trials`
aggregates on `trial_start_date`) while `new_subscriptions` is event-dated
(`fct_mrr_movements` on `event_date`), so last week's cohort books this week.
RCA's `parent_windows` surfaces the shifted window, which is what makes the
traverse-then-slice follow-up correct across the lag instead of quietly
comparing the wrong fortnight. (Until 2026-08-19 this edge was probabilistic
— and the generated data had no reactivation or direct path, so the fit was
learning an exactly deterministic edge: a point-mass posterior NUTS could
only flag as `suspect`. Roadmap S1's benchmark surfaced it.)

**The client-familiar learned edges, planted so they are learnable.** Trial
engagement causes conversion (`trial_conversion_rate` ←
`trial_activation_rate`, `trial_days_active` — cohort-contemporaneous, and
deliberately collinear: both ride the same underlying engagement, so the fit
sizes their *sum* more surely than the split, which is roadmap S4's caveat
demonstrated on purpose), and member activity suppresses churn
(`customer_churn_rate` ← `member_activity_rate`, negative declared sign). The
generator couples these for real: per-user engagement propensity plus shared
day-level drivers (`trial_engagement`, `member_engagement`) move activity and
the lifecycle probabilities together, mean-preservingly, and churn resolves
on weekly hazard sub-ticks so the co-movement survives to the week grain the
tree fits.

**Mixed grains, honestly declared.** MRR movements at `week`, funnel at `day`;
finer flow parents resample up by sum. `trial_conversion_rate`, `new_arpu`, and
`visit_signup_rate` are `kind: rate` — rates can never auto-aggregate, so each is
declared at the grain it is consumed at.

**`dimensions:` only where slicing is meaningful.** `sessions`, `signups`, and
`trials_started` get `country` / `device` / `channel`; the MRR-movement nodes get
`plan`. Rate nodes carry an explicit `weight` (`trial_conversion_rate` →
`trials_started`).

Two derived metrics must be added to the fake_companies semantic layer, mirroring
the existing `arpu`: `new_arpu` = `new_mrr / nullif(new_subscriptions, 0)` and
`churn_arpu` = `churned_mrr / nullif(churned_subscriptions, 0)`.

> MetricFlow dimension identifiers are `<primary_entity>__<dimension>` (e.g.
> `mrr_movement__plan`, `trial__country`). They are not guessable from the YAML
> alone — read them from `mf list dimensions --metrics <metric>` and let
> `breakdown doctor` confirm each one resolves.

## 5. Runtime shape: build live, serve hermetic

The deployed container ships **breakdown + the tree + committed parquet
snapshots**, and nothing else — no dbt, no DuckDB, no MetricFlow. It boots in
seconds and has no provider that can be down mid-pitch.

The full dbt → MetricFlow → Breakdown path is exercised at **build** time, by
`demo/Makefile`: generate the DuckDB, `dbt build`, `mf validate-configs`, then a
`breakdown serve --refresh` pass that populates `demo/.breakdown/snapshots/`.
That Makefile is also the artifact to screen-share when the pitch reaches "you
just point it at your dbt project" — the live semantic-layer story is more
convincing as a reproducible build than as a runtime dependency.

This requires one engine change: **sliced snapshot persistence**.
`SnapshotFetcher.fetch_metric_sliced` currently delegates straight to the inner
provider (deferred in the slicing design, §9), so slicing needs a live warehouse.
Implement it keyed `{metric}__{dim_source}__{grain}-{kind}__{start}__{end}`,
**stored once at the full loaded window and subset in memory on read** — keying
on the exact requested window would only serve the canned analyses, and a
prospect who picks their own dates would fall through to a provider that isn't
there.

## 6. Serving several prospects at once

Concurrent use already works. The only shared mutable state is
`app.state.traces`, which is a pure cache keyed `(metric, fit_end)`: two
prospects running RCAs at the same time get identical seeded results and warm
each other's cache. What-if interventions are client-side state posted to
`/simulate`. Nothing about this demo needs multi-tenancy in the engine.

Two things are added on top:

- **Pre-warming** (`demo/prewarm.py`) — POST the canned RCAs after boot so the
  trace cache is hot before the first visitor. Fits are serialized behind
  `app.state.lock`, so a cold multi-node RCA is the one real latency risk in a
  live pitch. Deliberately a script rather than an engine feature.
- **A localStorage workspace** — the UI already persists node-card display config
  under `breakdown.cardConfig`; the same pattern extends to
  `breakdown.workspace` for named saved what-if scenarios and saved RCA views.
  Zero backend, perfect isolation, and it survives a return visit. This is the
  right answer to "let clients keep their changes" precisely because the tree
  itself is read-only YAML — what a client actually creates is *analyses*, and
  those belong in their browser, not in our process.

## 7. Deployment

**Fly.io.** The existing `Dockerfile` deploys as-is; the demo variant just drops
dbt. `demo/fly.toml`: 2 GB VM (PyMC needs the headroom),
`auto_stop_machines = "suspend"` with `min_machines_running = 1`, health check on
`GET /health`, forced HTTPS.

**Suspend, not stop** is the load-bearing setting: it snapshots RAM, so the
pre-warmed trace cache survives an idle period and wakes in about a second.
Stopping would cold-boot and re-fit on the first click.

**Amended 2026-08-05 — `min_machines_running` 0 → 1.** Suspend only covers short
idles; Fly stops a long-idle machine outright, and that path measured **16.0s**
to first byte on `GET /ui` (0.56s warm). Acceptable while the demo was
invite-only and every visitor was expected; not acceptable once the URL ships in
the v0.1.0 release notes and the PyPI sidebar, where the person paying the 16s
is by definition the one who has never seen the tool. The §7 scaling note below
("invite-only → ~$0 idle") is superseded for this reason.

Env: `BREAKDOWN_PUBLIC_URL` (so MCP `report_url` deep links resolve to the public
hostname), `BREAKDOWN_START_DATE` / `BREAKDOWN_END_DATE`, and
`BREAKDOWN_API_TOKEN` as a secret.

**`/mcp` needs a gate.** It is unauthenticated today — fine on loopback, not fine
on the public internet. Add a small ASGI middleware, active only when
`BREAKDOWN_API_TOKEN` is set, checking `Authorization: Bearer …` on `/mcp`.
Clients connect with
`claude mcp add --transport http breakdown <url> --header "Authorization: Bearer …"`.
This is a down payment on roadmap 3.5's hosted mode, not throwaway demo code.

Scaling: ~~invite-only → one suspended machine, ~$0 idle~~ (superseded — see the
2026-08-05 amendment above; one machine now stays up so the public link never
cold-starts). Moderate traffic → add a region. A named prospect who should see their
own branded tree → a second machine or app with a different tree file; no code
change. Cloudflare Workers cannot run PyMC/pytensor and are not an option for
this stack.

## 8. Deferred: the second pass on the data

Once the demo is live, these re-scope stories A and B to what was originally
sketched, and each is a genuine improvement to `fake_companies` rather than
demo-only scaffolding:

- **`app_version` as a first-class segment dim**, with a time-varying release
  rollout. Touches config schema, three entity generators, the raw table specs,
  the DQ registry, and dbt. `mix` is static today, so the rollout needs a new
  time axis.
- **Generalize segment-aware driver lookup** out of `entities/funnel.py` into a
  shared helper used by `trial_start_rate`, `trial_convert`, and `churn.*`. This
  is the fix that makes `segment:` mean what the config schema already implies —
  today it validates and then silently does nothing on most drivers.
- **User geography on the MRR marts** — join `dim_users` into
  `fct_mrr_movements` and `fct_subscription_days`, expose `country` (and a
  `region` rollup) as semantic dimensions, so MRR is sliceable by geography.
- **Tighten the 30-day lifecycle bucket** so churn anomalies land inside their
  configured window.

## 9. How we know it works

The demo is protected by an assertion, not by a walkthrough:
`tests/test_white_cube_demo.py` runs each canned RCA against the committed
snapshots and asserts the top ranked cause is the metric the injected driver maps
to, and that the slice endpoint returns the injected segment (mobile for A,
professional for B, BR for C). `fake_companies` already records that mapping in
`meta.ground_truth` and `affected_metrics`, so the test compares against the
generator's own truth rather than against a number someone wrote down.

Everything else — `breakdown doctor` all-PASS, `mf validate-configs`, the guided
tour in [`demo_guided_tour.md`](demo_guided_tour.md) — is a check on top of that.

---

*This document is written and maintained by an AI agent (Claude), with human oversight.*
