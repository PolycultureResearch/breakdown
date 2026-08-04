# The White Cube guided tour

The client-facing script for the demo instance. Design rationale lives in
[`white_cube_demo_plan.md`](white_cube_demo_plan.md).

**What the prospect is looking at.** White Cube is a B2C subscription app for
visual artists — organize an archive of digital images and files, track what
showed in which exhibition, log what sold. Freemium with a 7-day trial; plans are
`studio` ($12/mo) and `professional` ($29/mo). The data is synthetic, generated
bottom-up from a simulated business, and every anomaly in it was planted on
purpose — so every answer below is checkable against ground truth.

Data window: **2024-06-01 → 2026-07-30**. Every window pair below is a whole
Monday→Sunday four-week block, which matters because the MRR layer of the tree
sits at week grain.

Every number quoted here was read off a real run against the committed
snapshots, and is asserted in `tests/test_white_cube_demo.py`. If a number here
stops matching what the demo shows, that test should already be red.

---

## 0. Open with the tree (30 seconds, no clicking)

`https://<demo-host>/ui`

Say: *this picture is a YAML file* — 18 metrics, about 150 lines.
`net_new_mrr` at the top decomposes into new, expansion, contraction and churned
MRR: an exact arithmetic identity, so attribution across it is exact Shapley
rather than a regression. Below that the funnel edges are probabilistic and carry
credible intervals. Solid edges are identities; dashed ones are learned.

Point at the `trial_conversions → new_subscriptions` edge: it carries a
**one-week lag**, because that is the trial. Breakdown compares that parent over
a window shifted back by the trial period rather than over the same calendar
fortnight.

Worth saying once, early: the apex is net *new* MRR, not MRR. Explaining the
level of a stock is not a well-posed question; explaining the flow that moves it
is.

---

## Story A — "New MRR fell in February. Why?"

*The setup, which you do not say out loud yet: a release broke the signup
call-to-action on mobile for paid-social landing pages, 2026-02-02 → 2026-03-15.*

**Run it.** Target `new_mrr` · Reference **2026-01-05 → 2026-02-01** · Analysis
**2026-02-09 → 2026-03-08**.

**What it says.** New MRR is down **−14.8%** (−$252/week). The tree walks it back
through `new_subscriptions` (−18.7%) and `trial_conversions` (−12.3%) to
`signups` (−10.2%) — not to price, not to conversion quality:

- On `new_mrr`, `new_subscriptions` carries **129.6%** of the gap while
  `new_arpu` carries **−24.7%**. Average deal size actually *rose* and masked a
  quarter of the damage. Breakdown does not clamp shares to 100%, so an
  offsetting effect shows up as a number instead of being flattened away.
- On `trial_conversions`, `trials_started` carries ~101% and
  `trial_conversion_rate` ~2%. It is a volume problem, not a quality one.
- `unexplained` on `new_mrr` is −2.3e-13. The identity is exact; nothing is
  hiding in the remainder.

**Note the lag.** The analysis window starts a week *after* the break. Open the
`new_subscriptions` block: its contribution from `trial_conversions` is tagged
`lag 1`, and the parent window it actually compared is **2026-02-02 →
2026-03-01** — the anomaly's exact start date. That is the trial period, modelled.

**Then slice.** On the `signups` cause, click **slice by → device**:

> mobile carries **90.1%** of the gap on a 51.9% baseline share.

Now click **country** on the same cause:

> Not localized by country — no slice carries enough of the gap beyond its own
> size to single it out.

That contrast is the point of the whole demo. Slicing localizes, it does not
explain — and it says so when there is nothing there, instead of naming whatever
sorted first.

**The line:** the tree said *which metric*, the slice said *which segment*, and
the lag meant it compared the right fortnight. No flat slicer has the middle
step, because it has no tree to traverse.

---

## Story B — "Net new MRR dropped in May, but acquisition looked fine."

*Setup: churn among professional-tier subscribers spiked, 2026-04-27 → 2026-06-28.*

**Run it.** Target `net_new_mrr` · Reference **2026-03-16 → 2026-04-12** ·
Analysis **2026-05-11 → 2026-06-07**.

**What it says.** Net new MRR is down **−32.1%**, and the graph splits in two
colours: the churn branch is red (`churned_mrr` **+87.4%**,
`churned_subscriptions` +58.6%, `customer_churn_rate` +45.2%) while the entire
acquisition branch is green (`new_mrr` +11.2%, `new_subscriptions` +50.3%).
Acquisition was having a good month and partly hid the problem.

**Then slice** `churned_mrr` by **plan**:

> professional carries **79.9%** of the gap on a 39.6% baseline share.

Slice `customer_churn_rate` by **country** for the contrast: not localized, every
row flagged `noise`. The problem is a tier, not a geography.

**The line:** the headline moved once; two branches moved hard in opposite
directions. That is exactly what a single-number dashboard cannot show you, and
it is why the offsetting contribution is reported rather than netted away.

---

## Story C — "Something good happened in the spring."

*Setup: a Brazil-targeted campaign — paid-social spend ramped 35%, and it
converted unusually well in Brazil, 2025-03-03 → 2025-05-25.*

**Run it.** Target `signups` · Reference **2025-02-03 → 2025-03-02** · Analysis
**2025-03-10 → 2025-04-06**.

**What it says.** Signups up **+8.1%**, split across both halves of the identity
`signups = sessions × visit_signup_rate`: `sessions` carries **66.5%** (the spend
bought traffic) and `visit_signup_rate` carries **35.9%** (the traffic converted
better than usual). Sessions themselves are only +4.9%, so this is not just
volume.

**Then slice** `signups` by **country**:

> BR carries **64.5%** of the gap on an 8.7% baseline share.

**The line:** run this one to show RCA is not a bad-news tool. The same
decomposition tells you which half of a win was volume and which was quality —
which is what you need in order to decide whether to spend more.

---

## Story D — "The onboarding revamp — did it work?"

*Setup: a trial-conversion lift, 2025-08-04 → 2025-11-30.*

**Run it.** Target `new_mrr` · Reference **2025-07-07 → 2025-08-03** · Analysis
**2025-08-11 → 2025-09-07**.

**What it says.** New MRR up, and on `trial_conversions` the split runs the
opposite way to story A: `trial_conversion_rate` outweighs `trials_started`. The
same number of people started trials; more of them converted.

**Worth saying:** the windows here are deliberately adjacent. White Cube is in
the steep part of its growth curve, and an eight-week gap would carry ~25%
underlying growth in trial volume — enough that the tree would have credited
volume rather than the change. Choosing comparable windows is a real part of
using this well, and the tool will faithfully attribute trend if you hand it a
trendy comparison.

---

## What-if — "So what should we do about the churn?"

Open the **What-if** tab after story B.

1. Baseline window: **2026-06-29 → 2026-07-26** (a recent clean stretch).
2. Intervene on `customer_churn_rate`: **−20%**.
3. Run. `churned_mrr` falls, `net_new_mrr` rises by roughly 20% of the churn the
   business had been losing, with `prob_direction` near 1.

Then push it: set the cut to **−300%**. The engine flags `non_physical` —
a negative churn rate is arithmetic, not a business. Say that out loud: *it is
telling you the scenario is nonsense rather than returning a confident number.*

Second scenario worth running: `marketing_spend` +30%. The effect on
`net_new_mrr` arrives through the funnel *and through the lag*, with a much wider
interval than the churn lever — buying growth is less certain than keeping
customers, and the posterior says so.

---

## The MCP demo — same engine, from Claude

```bash
claude mcp add --transport http breakdown https://<demo-host>/mcp \
  --header "Authorization: Bearer <token>"
```

Then, in order:

1. *"What does this metric tree measure?"* — calls `get_tree`; establishes that
   the assistant is reading a real DAG, grains and all.
2. *"Why did new MRR fall in February 2026?"* — calls `run_rca`. The answer
   should name the signup drop, cite the credible interval, and mention the
   unexplained remainder rather than hiding it.
3. *"Which segment?"* — calls `slice_metric`, should come back with mobile, and
   should use the lag-shifted windows to do it.
4. *"What if we recovered that signup rate?"* — calls `run_whatif`.

Every response carries a `report_url` that replays the exact analysis in the UI.
Open one: the numbers match, because the engine is seeded. That is the moment
worth pausing on — the chat answer and the interactive report are the same
analysis, not two systems that approximately agree.

**Point to make:** the assistant is not summarizing a dashboard. Each claim in
its answer maps to a field in the tool response — the interval, the offsetting
contribution, the honest remainder, the "this tree cannot say why" caveat.

---

## Letting them keep something

**Share → Save this view** stores the current analysis under a name. It lives in
their browser only: nothing is uploaded, other people on the same demo never see
it, and it is still there when they come back. Worth demonstrating at the end of
a call — save the two RCAs you just ran so they can re-open them cold.

**Share → Copy link** yields the same thing as a URL, which is the version to
paste into an email.

---

## Closing: how the tree got written

Show `demo/AUTHORING.md` — Claude Code in a dbt repo, reading the semantic
manifest, writing the tree YAML. The pitch is that authoring is not the hard
part; having a semantic layer worth pointing at is. If the prospect has one,
they are most of the way there.

---

## If something goes wrong

- **Slow first analysis** — the trace cache went cold (the machine suspended for
  a long idle, or was redeployed). Re-run; the second is instant. Before a
  scheduled call, run `python demo/prewarm.py --rcas --url https://<host>`.
- **A node reports `window_shorter_than_grain`** — the chosen window holds no
  whole week. Use the four-week pairs above.
- **A slice says "not localized"** — that is a real answer, not a failure. Say
  so; it is the behaviour that makes the localized ones worth believing.
