# Fixtures for `read-the-numbers`

Per-tree operational reference: what each tree can surface, how to run it, the
**shape** a correct run has, and what is already known-wrong on it.

Deliberately not here: exact expected values (they live in the tests that pin
them, which go red when they move — a number restated here would drift
silently), defect descriptions (the roadmap row is the source of truth), and the
tells for reading output (`SKILL.md`).

Read the known-bad line before reporting a finding. Rediscovering a logged
defect is noise; worse, a fixture with an unlabelled known-bad trains you to
ignore red flags.

| Tree | Metrics | Grain | Provider | What it is for |
|---|---|---|---|---|
| **White Cube** `demo/white_cube_tree.yml` | 23 | day + week | committed snapshots | **Ground truth.** Anomalies planted on purpose; every answer checkable. |
| **B2B MRR** `knowledge/b2b_mrr_tree.yml` | 106 | day + month | mock | **Scale and shape.** Mixed grain, 26 sliceable nodes, deep chains. |
| **Jaffle** `breakdown/examples/jaffle_shop_tree.yml` | 4 | day | mock | Fast sanity check only. Too small to surface much. |

Two trees is the current standard — White Cube plus one other. As more demo
trees are built for prospects, add a section here.

---

## White Cube — the ground-truth fixture

Synthetic B2C subscription app, generated bottom-up from a simulated business
with `fake_companies`. Data **2024-06-01 → 2026-07-30**. The MRR layer sits at
week grain, so **every window pair must be whole Monday→Sunday blocks**.

```bash
BREAKDOWN_SNAPSHOT_DIR=demo/.breakdown/snapshots \
WHITE_CUBE_DBT_PROJECT=/nonexistent/white-cube-has-no-provider \
uv run breakdown serve --tree demo/white_cube_tree.yml \
  --start-date 2024-06-01 --end-date 2026-07-30
```

**Both env vars are required and the second one looks wrong on purpose.** The
tree declares a `dbt` provider whose `project_path` is
`${WHITE_CUBE_DBT_PROJECT}`, so parsing fails outright without it — the tree
shows as errored and every route 503s. Pointing it at a nonexistent path is what
`tests/test_white_cube_demo.py` does deliberately: if anything reaches a
provider instead of a snapshot, it fails loudly rather than quietly working on a
machine that happens to have the dbt project. Copy the command; do not "fix" the
path.

Snapshots are committed (`demo/.breakdown/snapshots`, 59 files: 23 metrics + 36
sliced, plus the manifest). If missing, `make -C demo snapshots` — which regenerates
both halves and, since roadmap 1.11, actually completes: it had been unable to
finish since C2 landed on 2026-08-05, invisibly, because the snapshots were
committed.

**Four planted stories**, each with a known cause. Full script and the expected
narrative: [`knowledge/demo_guided_tour.md`](../../../knowledge/demo_guided_tour.md),
authoritative for the windows.

**The tour's percentages are pinned now — and step 6 is still the reason to
check them.** This line used to say the opposite, and it was right when written:
`tests/test_white_cube_demo.py` pinned only *properties* (gap sign, which node
ranks first, the lag, a share inequality) while the tour quoted percentages that
had drifted, one by 28 points. Every figure the tour prints is now pinned to the
decimal place it prints, **beside** the property it is the witness for, and the
`prints()` helper fails in both directions — engine drift and a hand-edited
document alike. So the failure mode step 6 is hunting has changed shape rather
than gone: a *new* figure added to the tour and never pinned, or a re-pin that
moved a value without re-reading the sentence it sits in. Read the paragraph,
not just the number.

| Story | Target | Reference | Analysis |
|---|---|---|---|
| A — new MRR fell in February | `new_mrr` | 2026-01-05 → 2026-02-01 | 2026-02-09 → 2026-03-08 |
| B — net new MRR dropped, acquisition looked fine | `net_new_mrr` | 2026-03-16 → 2026-04-12 | see tour |
| C — something good happened in spring | `signups` | 2025-02-03 → 2025-03-02 | see tour |
| D — did the onboarding revamp work | `new_mrr` | 2025-07-07 → 2025-08-03 | see tour |

**Shape of a correct Story A run** — properties, so they survive a re-seed or a
sampler change that moves the values:

- `new_mrr` is an exact identity over two parents, so `unexplained` is ~0 and
  the two shares sum to 1.000.
- The gap is negative and double-digit percent.
- **Volume is established, rate is not**: the `new_subscriptions` interval
  excludes zero with `prob_same_direction` ≈ 1; `new_arpu`'s spans zero. Shares
  exceed 100% because the two parents offset — correct, not a defect.
- `ranked_causes[0]` is `new_subscriptions`, reached via `new_mrr`.

If the top cause moves off `new_subscriptions`, that is a finding regardless of
what the tests say — the planted cause is a volume story.

**`churn_arpu` is the undefined-rate fixture.** Eleven of its 112 weeks have no
value — every one of them a week in which nobody churned, so the rate is `0/0`
(the tree declares `denominator: churned_subscriptions`, which is what lets the
engine say *undefined* rather than *missing*). The business is barely a month old
at the start of the window, which is why they cluster there. Re-measured
2026-08-22 off the committed snapshot; the previous counts here predated the
engagement-edges regeneration and named a week (`2024-09-02`) that is now
defined, and its behaviour bullets predated 1.12's up-front refusal. All three
cases below were re-run against the live server on 2026-08-22:

- `2024-06-03` → `2024-08-05` is **ten consecutive** undefined weeks. A window
  wholly inside it makes `churned_mrr` (`churned_subscriptions * churn_arpu`)
  non-finite, and `POST /rca/churned_mrr` **refuses with a 422** that names the
  formula, which side of the comparison failed, and how many periods —
  *"`churned_subscriptions` is zero or non-finite on 4 of 4 reference-window
  week(s)"*. A refusal, not a degraded node: the message is the fixture.
- `2024-08-19` is the lone undefined week, with `2024-08-12` and `2024-08-26`
  defined either side — use it as a single-period **analysis** window against
  `2024-08-12` → `2024-08-18` and the same refusal fires on the analysis side
  (*"1 of 1 analysis-window week(s)"*), which is how you check the message
  reports the correct half.
- A window merely *containing* an undefined week is fine: `2024-10-07` →
  `2024-11-03` reports `status: ok` on `churn_arpu`, `churned_mrr` and
  `churned_subscriptions` alike, because the window aggregate is
  `Σchurned_mrr / Σchurned_subscriptions` rather than a mean of weekly ratios.
  That contrast — refused when *every* period is undefined, ordinary when only
  some are — is the property worth re-checking, in both directions.

Every tour window is well clear of all eleven, so the four stories are
unaffected.

**Known-bad:** none currently logged.

---

## B2B MRR — the scale and shape fixture

The worked reference tree. Mock provider: deterministic, but **not** ground
truth — do not check specific values against a narrative here. Run it to see
whether anything breaks structurally at 106 metrics: mixed day/month grain,
three documented day→month handoffs, 46 rates, 6 stocks, dimensions on 26 nodes.

```bash
uv run breakdown serve --tree knowledge/b2b_mrr_tree.yml \
  --start-date 2024-01-01 --end-date 2024-12-31
```

**Use it for:** grain handoffs, wide formula nodes, slicing, and anything that
scales with metric count. It is also the only tree where a monthly node's fit
window is realistically short.

**Known-bad, stated as a cause rather than a symptom — the cause generalizes and
the symptom does not.** The mock derives every formula node from its parents
**plus ~2% noise**, so on this tree **no formula identity holds exactly**. The
one consequence you will meet, and it is expected:

- Any node reporting a modest `unexplained` on what the YAML says is an exact
  identity — e.g. `total_arr = total_mrr * 12` — is the fixture's noise, not an
  engine bug (measured 2026-08-17: ~1–5% relative residual per period).

**No longer known-bad:** `controllable_attrition` used to be *negative in every
period* — its two leaves were drawn at unrelated scales, so "saved" always
exceeded "requested". Fixed 2026-08-17 (roadmap
[**C13**](../../../knowledge/roadmap.md): the subtrahend of a plain `a - b`
difference is generated as a varying share of the minuend), and pinned by
`tests/test_reference_tree.py`, so `saved_cancel_requests ≤ cancel_requests`
now holds per period. A negative `controllable_attrition` here is a **finding**
now, not a known-bad. The constraint covers only a plain leaf−leaf difference
at one grain — a multi-term difference on some future tree can still go
negative; the mock's docstring states the scope.

Do not report the identity noise as new. Everything *structural* is still fair
game: a grain handoff that drops periods, a refusal that fires wrongly, a slice
that will not reconcile. Judge those on their own terms — the fixture's noise
explains wrong *values*, not wrong *behaviour*.

---

## Jaffle — the fast sanity check

Four metrics, day grain, mock. Runs in seconds. Use it when you want to confirm
a change did not break the basic path, not to find anything subtle.

Its one useful window is the README's MCP transcript — reference
`2024-03-13 → 2024-03-26`, analysis `2024-03-27 → 2024-04-09`, target `revenue`
— pinned by `tests/test_readme.py`.

**Shape:** `revenue = order_count × average_order_value` is an exact identity,
so `unexplained` is ~0; the two shares offset well past 100%; and **neither
leg's direction is established** over 14 periods. That last one is deliberate —
it is C4's corrected intervals, not a regression. A run here that reports a
confident direction on a fortnight is a finding.
