# The White Cube guided tour

The client-facing script for the demo instance. Design rationale lives in
[`white_cube_demo_plan.md`](white_cube_demo_plan.md).

**What the prospect is looking at.** White Cube is a B2C subscription app for
visual artists — organize an archive of digital images and files, track what
showed in which exhibition, log what sold. Freemium with a 7-day trial; plans are
`studio` ($12/mo) and `professional` ($29/mo). The data is synthetic, generated
bottom-up from a simulated business, and every anomaly in it was planted on
purpose — so every answer below is checkable against ground truth.

Data window: **2024-06-01 → 2026-07-30**. Every RCA window below is a whole
Monday→Sunday four-week block, which matters because the MRR layer of the tree
sits at week grain.

> Status: window pairs are fixed by the scenario config; the expected magnitudes
> in each "should say" are filled in from the first verified build.

---

## 0. Open with the tree (30 seconds, no clicking)

`https://<demo-host>/ui`

Say: *this picture is a YAML file.* `net_new_mrr` on top decomposes into new,
expansion, contraction and churned MRR — an exact arithmetic identity, so
attribution across it is exact Shapley, not a regression. Below that the funnel
edges are probabilistic and carry credible intervals. Solid edges are identities;
the rest are learned.

Point at the `trial_conversions → new_subscriptions` edge: it carries a **one-week
lag**, because that is the trial. Breakdown compares that parent over a window
shifted back by the trial period rather than over the same calendar fortnight.

---

## Story A — "New MRR fell in February. Why?"

*The setup, which you do not say out loud yet: a release broke the signup
call-to-action on mobile for paid-social landing pages, 2026-02-02 → 2026-03-15.*

**Run it.** Target `new_mrr` · Reference **2026-01-05 → 2026-02-01** · Analysis
**2026-02-09 → 2026-03-08**.

**Should say:** new MRR is down, and the tree walks it back past
`new_subscriptions` and `trial_conversions` to `signups` — not to price, not to
conversion quality. Note that the analysis window starts a week *after* the break:
that is the trial lag, and the ranked cause carries the shifted parent window.

**Then slice.** On the `signups` cause, slice by **device** → mobile carries far
more of the gap than its baseline share. Slice by **country** → nothing
concentrates, because the break was not geographic. That contrast is the point:
slicing localizes, it does not explain, and it is honest when there is nothing
there.

**The line:** tree RCA said *which metric*, slicing said *which segment*, and the
lag meant it compared the right fortnight. No competitor has the middle step,
because a flat slicer has no tree to traverse.

---

## Story B — "Net new MRR dropped in May, but acquisition looked fine."

*Setup: churn among professional-tier subscribers spiked, 2026-04-27 → 2026-06-28.*

**Run it.** Target `net_new_mrr` · Reference **2026-03-16 → 2026-04-12** ·
Analysis **2026-05-11 → 2026-06-07**.

**Should say:** the gap is on the `churned_mrr` branch, not the `new_mrr` branch —
acquisition was healthy and partially offset the damage. Watch the offsetting
contribution: Breakdown does not clamp shares to 100%, so a masking effect shows
up as a positive contribution against a negative gap instead of being flattened.

**Then slice** `churned_mrr` by **plan** → professional. Follow with
`churned_subscriptions` by plan for the same story in units of customers rather
than dollars.

**The line:** the headline metric moved a little; two branches moved a lot in
opposite directions. That is the failure mode a single-number dashboard cannot
show you.

---

## Story C — "Something good happened in the spring."

*Setup: a Brazil-targeted campaign — paid-social spend ramped 35%, and it
converted unusually well in Brazil, 2025-03-03 → 2025-05-25.*

**Run it.** Target `signups` · Reference **2025-01-27 → 2025-02-23** · Analysis
**2025-03-31 → 2025-04-27**.

**Should say:** signups up, attributed across both `sessions` (the spend bought
traffic) and `visit_signup_rate` (the traffic converted better than usual) —
the identity `signups = sessions × visit_signup_rate` splits volume from quality
exactly.

**Then slice** `signups` by **country** → Brazil carries several times its
baseline share of the lift. Slice by **channel** → paid-social.

**The line:** run this one to show RCA is not a bad-news tool. The same
decomposition tells you which half of a win was volume and which was quality,
which is what you need to decide whether to spend more.

---

## Story D — "The onboarding revamp — did it work?"

*Setup: a trial-conversion lift of ~25%, 2025-08-04 → 2025-11-30.*

**Run it.** Target `new_mrr` · Reference **2025-06-30 → 2025-07-27** · Analysis
**2025-08-25 → 2025-09-21**.

**Should say:** new MRR up, attributed to `trial_conversion_rate` rather than to
`trials_started` — the same number of people started trials, more of them
converted. This is the cleanest "did our change work" read in the tour: the tree
separates the effect of the change from the volume it happened to arrive with.

---

## What-if — "So what should we do about the churn?"

Open the **What-if** tab after story B.

1. Set the baseline window to a recent clean stretch, e.g. **2026-06-29 →
   2026-07-26**.
2. Intervene on `customer_churn_rate`: −20%.
3. Run. Read the posterior on `net_new_mrr` — a range, with the probability the
   direction is positive, not a point estimate.
4. Watch for the amber extrapolation flag if the intervention pushes a node
   outside its historical range. Say so out loud when it appears: *the model is
   telling you it has never seen this, and it is refusing to pretend otherwise.*

Second scenario worth running: intervene on `marketing_spend` +30% and note that
the effect on `net_new_mrr` arrives through the funnel *and through the lag*, with
a much wider interval than the churn lever — buying growth is less certain than
keeping customers, and the posterior says so.

---

## The MCP demo — same engine, from Claude

Connect:

```bash
claude mcp add --transport http breakdown https://<demo-host>/mcp \
  --header "Authorization: Bearer <token>"
```

Then, in Claude, in order:

1. *"What does this metric tree measure?"* — calls `get_tree`; sets up that the
   assistant is reading a real DAG, grains and all.
2. *"Why did new MRR fall in February 2026?"* — calls `run_rca`. The answer should
   name the signup drop, cite the credible interval, and mention the unexplained
   remainder rather than hiding it.
3. *"Which segment?"* — calls `slice_metric` and should come back with mobile.
4. *"What if we recovered that signup rate?"* — calls `run_whatif`.

Every response carries a `report_url` that replays the exact analysis in the UI.
Open one: the numbers match, because the engine is seeded. That is the moment
worth pausing on — the chat answer and the interactive report are the same
analysis, not two systems that agree approximately.

**Point to make:** the assistant is not summarizing a dashboard. Each claim in its
answer maps to a field in the tool response — the interval, the offsetting
contribution, the honest remainder, the "this tree cannot say why" caveat.

---

## Closing: how the tree got written

Show `demo/AUTHORING.md` — Claude Code in a dbt repo, reading the semantic
manifest, writing the tree YAML. The pitch is that the hard part is not the
authoring; it is having a semantic layer worth pointing at. If the prospect has
one, they are most of the way there.

---

## If something goes wrong

- **Slow first analysis** — the trace cache went cold (the instance suspended for
  a long idle). Re-run; the second one is instant. Pre-warm with
  `demo/prewarm.py` before a scheduled call.
- **A node reports `window_shorter_than_grain`** — a chosen window holds no whole
  week. Use the four-week pairs above.
- **A slice reports `noise_level`** — the gap is not concentrated in any one
  slice. That is a real answer, not a failure; say so.
