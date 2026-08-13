# Fixtures for `read-the-numbers`

Which tree to run, what each one is *for*, and what is already known to be wrong
on it. Read the known-bad column before reporting a finding — rediscovering a
logged defect is noise, and worse, a fixture with an unlabelled known-bad trains
you to ignore red flags.

| Tree | Metrics | Grain | Provider | What it is for |
|---|---|---|---|---|
| **White Cube** `demo/white_cube_tree.yml` | 18 | day + week | committed snapshots | **Ground truth.** Anomalies planted on purpose; every answer checkable. |
| **B2B MRR** `knowledge/b2b_mrr_tree.yml` | 106 | day + month | mock | **Scale and shape.** Mixed grain, 26 sliceable nodes, deep chains. |
| **Jaffle** `breakdown/examples/jaffle_shop_tree.yml` | 4 | day | mock | Fast sanity check only. Too small to surface much. |

Two trees is the current standard — White Cube plus one other. As more demo
trees are built for prospects, add them here.

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

Snapshots are committed (`demo/.breakdown/snapshots`, 53 files), so this needs
no warehouse and no `mf`. If they are missing, `make -C demo snapshots`.

**Both env vars are required and the second one looks wrong on purpose.** The
tree declares a `dbt` provider whose `project_path` is `${WHITE_CUBE_DBT_PROJECT}`,
so parsing fails outright without it — the tree shows as errored and every route
503s. Pointing it at a nonexistent path is what `tests/test_white_cube_demo.py`
does deliberately: if anything reaches a provider instead of a snapshot, it fails
loudly rather than quietly working on a machine that happens to have the dbt
project. Copy the command; do not "fix" the path.

**Four planted stories**, each with a known cause. Full script with the expected
narrative in [`knowledge/demo_guided_tour.md`](../../../knowledge/demo_guided_tour.md);
the numbers are asserted in `tests/test_white_cube_demo.py`, so if one stops
matching, that test should already be red.

| Story | Target | Reference | Analysis |
|---|---|---|---|
| A — new MRR fell in February | `new_mrr` | 2026-01-05 → 2026-02-01 | 2026-02-09 → 2026-03-08 |
| B — net new MRR dropped, acquisition looked fine | `net_new_mrr` | 2026-03-16 → 2026-04-12 | see tour |
| C — something good happened in spring | `signups` | 2025-02-03 → 2025-03-02 | see tour |
| D — did the onboarding revamp work | `new_mrr` | 2025-07-07 → 2025-08-03 | see tour |

**Story A, measured 2026-08-13** — the anchor to compare against:

```
new_mrr           1701.5 → 1449.6   gap -251.8 (-14.8%)   unexplained -0.00
  new_subscriptions   -324.8   share +1.290   ci [-534.8, -76.8]   psd 0.998
  new_arpu             +73.0   share -0.290   ci [-168.3, 293.1]   psd 0.708
ranked[0] new_subscriptions 0.633 via new_mrr
```

Read it the way the practice asks: `unexplained` is −0.00 because `new_mrr` is
an exact identity over the two parents, shares sum to 1.000, volume is
established (psd 0.998, interval excludes zero) and rate is not (psd 0.708,
interval spans zero). Shares exceeding 100% are correct here — the two parents
offset.

**Use it for:** does the engine still recover the planted cause? If the top
cause moves off `new_subscriptions`, that is a finding regardless of what the
tests say.

**Known-bad:** none currently logged.

---

## B2B MRR — the scale and shape fixture

The worked reference tree. Mock provider, so data is deterministic but *not*
ground truth — do not check specific values against a story here. Run it to see
whether anything breaks structurally: mixed day/month grain, three documented
day→month handoffs, 46 rates, 6 stocks, dimensions on 26 nodes.

```bash
uv run breakdown serve --tree knowledge/b2b_mrr_tree.yml \
  --start-date 2024-01-01 --end-date 2024-12-31
```

**Use it for:** grain handoffs, wide formula nodes, slicing, and anything that
scales with metric count. Also the only tree where a monthly node's fit window
is realistically short.

### Known-bad: `controllable_attrition` is negative in every period

Roadmap **C13**, open. Measured 2026-08-13 over 2024-01-01 → 2024-12-31:

```
controllable_attrition   min = -3,043.0   max = -2,471.7   ← never positive
cancel_requests          min    924.8     max  1,220.2
saved_cancel_requests    min  3,357.4     max  4,214.8     ← always exceeds requests
```

`controllable_attrition = cancel_requests − saved_cancel_requests`, and the mock
draws both leaves independently at unrelated scales, so "saved" always exceeds
"requested" — semantically impossible, and negative in 100% of periods rather
than occasionally.

**Two corrections to C13's roadmap row, from running it:** the row says the
defect makes the difference "go negative", implying sometimes; it is *always*.
And the row says `churned_customers` / `churned_mrr` inherit it — they do not.
`churned_customers = controllable_attrition + uncontrollable_attrition` stays
positive (1,467.8 → 2,413.6) because the uncontrollable term is larger. Blast
radius is one node, not three.

Do not report this as new. Do treat any *other* negative flow or stock as a
finding: one known-bad node is the calibration, and a second means something
changed.

---

## Anchors worth knowing

**Jaffle**, ref `2024-03-13 → 2024-03-26`, analysis `2024-03-27 → 2024-04-09` —
the README's MCP transcript, pinned by `tests/test_readme.py`. `revenue`
26,386.52 → 26,982.07, gap +595.55. `order_count` share 1.6532, `average_order_value`
−0.6165. Neither leg's direction is established at 14 periods, deliberately —
that is C4's wider intervals, not a regression.

**A useful trap:** `ranked_causes[0]` on that run scores 0.4406, not 1.0. Before
C5 it was exactly 1.0 — a saturated clamp on the most prominent number in the
product, on the first tree a new user opens, missed by two hostile reviews. If a
change ever puts an exact 1.0 back there, that is the same defect returning.
