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

**What backs the numbers below.** Every figure here was read off the **UI** —
the RCA tab's *Headline* view and the slice panel's verdict line — on a real run
against the committed snapshots, and each one is pinned, to the decimal place
printed here, in `tests/test_white_cube_demo.py`. If a figure here stops
matching what the demo shows, that test is red.

Three things are deliberately **not** pinned, and each is marked where it
appears: the sentences an assistant writes in the
[MCP section](#the-mcp-demo--same-engine-from-claude) — the tool calls and the
fields behind them are pinned, the prose cannot be — and the **⚠ known gap**
notes, which are product defects filed separately and which the presenter must
not promise. Nothing else in this document is unasserted.

**The UI's shares and the API's differ slightly, on purpose.** The Headline
table gives the *co-movement* term its own row, so a driver's share there is its
window-means part over the gap. The API's `share_of_gap` folds each parent's
slice of that co-movement back into the parent, so it reads a little
differently: `new_subscriptions` in story A is **98.8%** on screen and `0.978`
in the payload. Both decompositions are complete, and both sum to the gap. Quote
the screen. If a prospect diffs this document against the API, that is the
reason, and it is not a bug.

---

## 0. Open with the tree (30 seconds, no clicking)

`https://<demo-host>/ui`

Say: *this picture is a YAML file* — **23 metrics** in **400 lines**, of which
284 are actual configuration and the rest comments and blanks.
`net_new_mrr` at the top decomposes into new, expansion, contraction and churned
MRR: an exact arithmetic identity, so attribution across it is exact Shapley
rather than a regression. Solid edges are identities; dashed ones are learned,
and the learned ones are the edges a subscription business actually argues
about: marketing spend → sessions, trial activation and days-active →
conversion, member activity → churn.

Point at `new_subscriptions`: every way into a paid plan is a term of an exact
identity — `trial_conversions [lag 1] + reactivations + direct_conversions` —
and the trial term carries a **one-week lag**, because that is the trial.
`trial_conversions` is dated by the week the trial *started*; the booking lands
a week later. Breakdown compares that parent over a window shifted back by the
trial period rather than over the same calendar four weeks — and because the
node keeps its own source, the identity is checked against the books at load:
`unexplained` on this node means the ledgers disagree, not "noise".

Worth saying once, early: the apex is net *new* MRR, not MRR. Explaining the
level of a stock is not a well-posed question; explaining the flow that moves it
is.

---

## Story A — "New MRR fell in February. Why?"

*The setup, which you do not say out loud yet: a release broke the signup
call-to-action on mobile for paid-social landing pages, 2026-02-02 → 2026-03-15.*

**Run it.** Target `new_mrr` · Reference **2026-01-05 → 2026-02-01** · Analysis
**2026-02-09 → 2026-03-08**.

**What it says.** New MRR is down **−16.7%** (−$324/week). The tree walks it back
through `new_subscriptions` (−16.5%) and `trial_conversions` (−18.9%) to
`signups` (−11.2%) — not to price:

- On `new_mrr`, `new_subscriptions` carries **98.8%** of the gap while
  `new_arpu` carries **3.2%** (co-movement −1.9%). The price axis is cleared
  in one row: the count fell, what each subscription was worth is a bystander.
- On `new_subscriptions`, the identity names every door into a paid plan:
  trial conversions carry **86.4%** of the drop, direct conversions **16.1%**
  (they fell too — fewer free users to convert), and reactivations **−2.5%**
  (they ticked up *against* the drop, and the sign is reported rather than
  netted away).
- On `trial_conversions`, `trials_started` carries **68.7%** against
  `trial_conversion_rate`'s **32.0%**. Mostly a volume problem — and the
  conversion wobble is not noise either: this tree can chase it into the
  trial-engagement branch, which is story D's machinery pointed the other way.
- `unexplained` on `new_mrr` and on `new_subscriptions` reads **0.0**. Both
  identities are exact; nothing is hiding in a remainder. (The test pins
  below-1e-9, not the digits.)

**Note the lag.** The analysis window starts a week *after* the break. The
engine compares `trial_conversions` not over the calendar window but over
**2026-02-02 → 2026-03-01**, shifted back a week — the anomaly's exact start
date. That is the trial period, modelled.

> **⚠ Known gap — do not point at the RCA table for this.** The RCA attribution
> table renders no `lag` and no parent window. There is no `lag 1` tag in the
> `new_subscriptions` block to point at; earlier drafts of this script said
> there was. The lag is visible in the UI in exactly two places, neither of them
> that table:
>
> - the **Metric** tab for `new_subscriptions`, whose parents list carries a
>   `lag 1 week(s)` chip on `trial_conversions` — the *declared* lag, not the
>   window it produced;
> - the **slice panel** on the `trial_conversions` cause. Click **slice by →
>   device** there and the panel's footer reads `2025-12-29 → 2026-01-25 vs
>   2026-02-02 → 2026-03-01 · windows shifted back 1 week for the lag` — the
>   actual shifted window, on screen.
>
> The API and MCP responses do carry both (`lag` and `parent_windows` on the
> contribution), which is where the date above comes from. Until the table
> renders it, script the slice-panel footer and say the shift was applied. Do
> not promise a tagged row in the RCA table; there isn't one.

**Then slice.** On the `signups` cause, click **slice by → device**:

> mobile carries **76.2%** of the gap on a **51.1%** baseline share.

Now click **country** on the same cause:

> Not localized by country — no slice carries enough of the gap beyond its own
> size to single it out.

(These two run over `signups`' own windows — the calendar block, `2026-01-05 →
2026-02-01` vs `2026-02-09 → 2026-03-08`. `signups` sits *below* the lagged edge
and is not itself a lagged parent, so nothing is shifted here; the panel's
footer says which windows it used, every time.)

That contrast is the point of the whole demo. Slicing localizes, it does not
explain — and it says so when there is nothing there, instead of naming whatever
sorted first.

**The line:** the tree said *which metric*, the slice said *which segment*, and
the lag meant it compared the right four weeks. No flat slicer has the middle
step, because it has no tree to traverse.

---

## Story B — "Net new MRR dropped in May, but acquisition looked fine."

*Setup: churn among professional-tier subscribers spiked, 2026-04-27 → 2026-06-28.*

**Run it.** Target `net_new_mrr` · Reference **2026-03-16 → 2026-04-12** ·
Analysis **2026-05-11 → 2026-06-07**.

**What it says.** Net new MRR is down **−38.5%**, and one branch owns it: the
churn side is red (`churned_mrr` **+84.8%**, `churned_subscriptions`
**+47.9%**, `customer_churn_rate` **+34.2%**) while acquisition is flat
(`new_mrr` −1.5%). The headline moved on retention alone — a single-number
dashboard would show the drop and nothing about which half of the business
did it.

Inside `churned_mrr`, the split is **63.6%** `churned_subscriptions` /
**36.3%** `churn_arpu` (co-movement **0.1%**): more cancellations, and the ones
cancelling were worth more than average.

**Point at the engagement edge being *cleared*.** `customer_churn_rate` has a
learned parent now — `member_activity_rate`, "disengaged members churn" — and
in this window the tree checks that explanation and declines it. Member
activity moved the **wrong way for the story**: it is *up* **+2.5%** (25.4% →
26.0% of members active on an average day) in the four weeks churn spiked. The
edge's declared sign is negative, so a rise predicts churn *falling* — its
contribution comes back at **−7.7%** of the gap, pulling against it rather
than explaining it, and the RCA's own interval on that contribution crosses
zero (`P(direction)` **≈0.84**). The obvious wrong story ("our members are
drifting away") is examined and rejected on screen; what remains is a
tier-shaped problem, which the slice below names.

**Two intervals here, and a prospect who opens the Metric tab will see both**,
so say which is which before they ask. The **coefficient** β is about **−0.036**
with a 95% HDI of roughly **[−0.06, −0.013]** — clear of zero. (These come off
an MCMC fit, so read them as "about"; the interval's far end moves in its last
digit between machines. What does not move is that it stays clear of zero, and
that is the claim.) The tree is *sure the edge
exists* and sure of its direction: disengagement really does drive churn in
this business. The **contribution** interval is the one that straddles zero,
because it also carries the uncertainty in how much activity actually moved
over four weeks, and activity barely moved. That is the honest shape of the
answer and a better beat than "the model isn't sure": the mechanism is real,
it just did not fire in this window, and the product distinguishes those two
things instead of collapsing them. Do not say "the posterior is unsure of the
sign" — the coefficient's posterior is not, and the screen says so.

*If a prospect asks where those numbers come from:* every fitted node here is
sampled with full MCMC (NUTS), which is breakdown's default and the reason an
RCA takes the minute it takes. The Metric tab shows `max R̂ · divergences ·
min ESS` and no PSIS k̂ at all, because there is no approximation to check.

That default was a decision, and `customer_churn_rate` is the node that made
the case. Run the same window with the fast mean-field approximation
(`?inference_method=advi`) and it scores PSIS k̂ **1.26**, well past the 0.7
bar — and it reports this same contribution as **−4.9%** with β's HDI at
[−0.053, **+0.005**]. A point estimate a third too small *and* a coefficient
interval that fails to exclude zero where the exact one does. The fast answer
was wrong in exactly the direction that would have weakened this beat, and
nothing but k̂ could tell. So the fast path is opt-in, and when you take it
every node it touches shows its k̂ beside the numbers.

*Worth knowing if a prospect presses on it:* this is a real edge declining a
real window, not an edge too weak to say anything either way. The generator
couples member activity to churn through a shared engagement driver, and story
D's branch is the same machinery answering *yes* on a window where the
mechanism did move. An engagement edge that never fires is worthless; the
point is that this one fires when the mechanism moved and abstains when it
did not.

> **⚠ Known gap — one node on the churn branch has no colour.** `churn_arpu`
> is up **+25.0%** and carries **36.3%** of the churn damage, and it renders
> **uncoloured** — arrow and percentage, no red tint — because
> `demo/white_cube_tree.yml` never declares a `direction` on it (see the
> Known-gaps section at the end; it used to render green, which is fixed).
> "The *entire* churn branch is red" is therefore not quite what the prospect
> is looking at. Say "the churn branch" and point at `churned_mrr` /
> `churned_subscriptions` / `customer_churn_rate`, which are all correctly
> red; if someone notices the grey node, the honest answer is that the tree
> never declared which way is good for that metric, and the product refuses
> to guess.

**Then slice** `churned_mrr` by **plan**:

> professional carries **100.6%** of the gap on a **44.0%** baseline share.

Slice the same node by **country** for the contrast: the panel returns
*Not localized by country*, and **seven of its nine rows** carry a `noise`
flag. Same metric, two dimensions, opposite verdicts — the problem is a tier,
not a geography.

*(Keep the contrast on `churned_mrr`, both dimensions on the one node: two
opposite verdicts side by side is the beat. If someone asks for the same cut on
`customer_churn_rate`, its country slice is worth showing as the **third**
verdict — *concentrated in the long tail*, because the only row clearing the
concentration bar is `everything else`, the four countries folded outside this
dimension's `top_k`. It is the honest answer rather than a hedge: enumerate all
twelve countries and the verdict falls to "not localized" — no single country
clears the bar. Read it as "the tail moved; raise `top_k` to see inside", never
as "everything else is the cause".)*

**The line:** the tree named the branch, cleared the tempting wrong
explanation with a posterior, and the slice named the tier. Three verdicts,
each checkable, none of them available from a single-number dashboard.

---

## Story C — "Something good happened in the spring."

*Setup: a Brazil-targeted campaign — paid-social spend ramped 35%, and it
converted unusually well in Brazil, 2025-03-03 → 2025-05-25.*

**Run it.** Target `signups` · Reference **2025-02-03 → 2025-03-02** · Analysis
**2025-03-10 → 2025-04-06**.

**What it says.** Signups up **+8.5%**, split across both halves of the identity
`signups = sessions × visit_signup_rate`: `sessions` carries **60.0%** (the spend
bought traffic) and `visit_signup_rate` carries **42.8%** (the traffic converted
better than usual). Sessions themselves are only **+5.0%**, so this is not just
volume — nearly half the win is conversion quality, which is the part a spend
dashboard cannot see.

**Then slice** `signups` by **country**:

> BR carries **84.3%** of the gap on an **8.4%** baseline share.

**The line:** run this one to show RCA is not a bad-news tool. The same
decomposition tells you which half of a win was volume and which was quality —
which is what you need in order to decide whether to spend more.

---

## Story D — "The onboarding revamp — did it work, and *why*?"

*Setup: an onboarding revamp that lifted trial engagement,
2025-08-04 → 2025-11-30. The conversion lift is downstream of it — which is
exactly what the tree should discover.*

**Run it.** Target `new_mrr` · Reference **2025-07-07 → 2025-08-03** · Analysis
**2025-08-11 → 2025-09-07**.

**What it says, pass one.** New MRR up **+26.0%**, and on `trial_conversions`
the split runs the opposite way to story A: `trial_conversion_rate` carries
**69.3%** against `trials_started`'s **29.8%**. More trials started too — the
business is growing — but the win is conversion.

**What it says, pass two — the beat this story exists for.** The conversion
lift itself has a cause the tree can name. `trial_conversion_rate` has two
learned parents, the ones a subscription company argues about in every
retro: did they *activate* (upload their first work), and how many days did
they actually use the trial. In the window: `trial_activation_rate` **+35.9%**,
`trial_days_active` **+76.1%**, conversion **+40.2%** — and the attribution
on `trial_conversion_rate` hands **about 70%** of the gap to activation with
`P(direction)` **0.998** and an interval clear of zero. (Say "about", and mean
it: this is one half of a deliberately collinear pair, so it is the number the
posterior ridge is least sure of — the same seeded analysis measures anywhere
from 68% to 70% depending on the machine's numeric library. What does not move
is that the interval stays clear of zero, and that activation carries more of
the gap than days-active does. Those are the claims.) The story reads
straight off the screen: *the revamp moved activation, and conversion
followed.*

**Then say the honest part, because it is a selling point.** The second
parent, `trial_days_active`, shows a wide interval that straddles zero. The
two engagement measures move together — an activated trialist is also an
active one — so the data determines their *combined* effect far more sharply
than the split between them. The tool says exactly that instead of
manufacturing precision: the total is sure, the split between two collinear
twins is not. An analyst who has been burned by regression coefficients will
recognize what is being done for them here.

**And it does not leave you to infer that from the interval.** Since 2026-08-27
(roadmap S4) the node's header carries the diagnosis by name — *⚠ parents move
together — the split is softer than the total (trial_activation_rate ↔
trial_days_active, r 0.86)* — and hovering it gives the sentence in full. Point
at it. Two wide intervals look like two weak findings; the chip is what says
they are one strong finding measured twice, and that reading either number
alone is the mistake. It fires on this node and nowhere else in the tree.

**Worth saying:** the windows here are deliberately adjacent. White Cube is in
the steep part of its growth curve. Push the reference back eight weeks —
**2025-05-12 → 2025-06-08** instead of the adjacent block — and `trials_started`
reads **+21.6%** against the same analysis window instead of **+15.5%**: six
points of pure trend that the tree would then have credited to the revamp.
Choosing comparable windows is a real part of using this well, and the tool will
faithfully attribute trend if you hand it a trendy comparison. (Adjacency is now
the default: leave the reference on **auto** and it is exactly the matched
adjacent block described here — this paragraph is the *why* behind that
default.)

---

## What-if — "So what should we do about the churn?"

Open the **What-if** tab after story B.

1. Baseline window: **2026-06-29 → 2026-07-26** (a recent clean stretch).
2. Intervene on `customer_churn_rate`: **−20%**.
3. Run. `churned_mrr` falls from **$804/week to $643/week (−20.0%)**, and
   `net_new_mrr` rises by exactly that amount — **+$161/week, +9.7%** — with
   `P(direction)` **1.0** and a zero-width 95% interval. That last part is not
   false confidence: the churn edge is an arithmetic identity, so once you
   assume the rate, there is nothing left downstream to be uncertain about.

Then push it: set the cut to **−300%**. The engine flags `non_physical` on
`customer_churn_rate`, `churned_subscriptions` and `churned_mrr` — a negative
churn rate is arithmetic, not a business. Say that out loud: *it is telling you
the scenario is nonsense rather than returning a confident number.*

**If someone asks whether it only catches the negative side, show them the
other one.** Clear the scenario and put `member_activity_rate` at **+300%**:
it simulates to **1.025**, and the engine flags `non_physical` there too —
102.5% of members active is not an ambitious quarter, it is more members than
exist. The floor comes from history ("never been negative"); the ceiling comes
from the tree, where the node declares `share: true` — a proportion cannot pass
1 whatever the history contains. Until roadmap C26 (2026-08-27) only the floor
existed, and this run reported the same 1.025 as merely *above the historical
max* — which is the sentence a prospect discounts.

Second scenario worth running: `marketing_spend` +30%. The effect on
`net_new_mrr` arrives through the funnel *and through the lag*, and comes back
with a 95% interval **tens of dollars wide**, against the churn lever's
zero-width one — buying growth is less certain than keeping customers, and the
posterior says so. That contrast is the thing to point at. This lever runs
through a learned edge, so the test pins the *contrast* — wide here, degenerate
there — rather than a point estimate a sampler nudge could move; do not read a
dollar figure off this one as if it were the churn number.

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
3. *"Which segment?"* — calls `slice_metric`, and should come back with mobile.
   Watch which windows it slices over: for `signups` the correct ones are the
   windows `run_rca` resolved for that node, because `signups` is not itself a
   lagged parent. The shift belongs to `trial_conversions`, whose contribution
   carries `parent_windows`; the tool documentation tells the assistant exactly
   that distinction, so a good answer respects it.
4. *"What if we recovered that signup rate?"* — calls `run_whatif`.

**Not pinned, and it cannot be.** The tests cover the tool calls and the fields
in their responses. They cannot cover the sentences an assistant wraps around
them. Read this section as *what it should do*, and check the `report_url`
rather than trusting the prose — which is the point being made anyway.

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

## Known gaps — read before the call

One thing this script used to claim is still false in the product rather than
in the numbers. **Do not promise it on a call.**

1. **`churn_arpu` is uncoloured, not red.** It declares no `direction` in
   `demo/white_cube_tree.yml`, so the canvas shows its movement — arrow and
   percentage — without a good/bad tint. It used to render **green**, an
   improvement, while carrying 27.3% of the churn damage in story B; that is
   fixed, because an undeclared direction now survives serialization as
   undeclared instead of defaulting to `up_is_good` before it reaches the UI.
   What remains is an authoring gap in this tree, not a claim by the product:
   the churn branch is not uniformly red because one of its nodes has never
   been classified.

*(The second gap listed here — that the RCA table rendered no lag — is fixed:
the live table carries a `lag` chip with the shifted windows in its tooltip,
and the export prints them in full.)*

---

## If something goes wrong

- **Slow first analysis** — the trace cache went cold (the machine suspended for
  a long idle, or was redeployed). Re-run; the second is instant. Before a
  scheduled call, run `python demo/prewarm.py --rcas --url https://<host>`.
- **A node reports `window_shorter_than_grain`** — the chosen window holds no
  whole week. Use the four-week pairs above.
- **A slice says "not localized"** — that is a real answer, not a failure. Say
  so; it is the behaviour that makes the localized ones worth believing.
- **A slice says "concentrated in the long tail"** — the third verdict, and the
  same restraint one step further in. The gap really is concentrated, but in
  `everything else`: the roll-up of the values outside the dimension's `top_k`,
  which is the set nobody enumerated. There is no segment to go and act on, so
  the panel says exactly that and tells you to raise `top_k` or slice another
  dimension. Do **not** read it out as "everything else is the cause" — the
  whole point of the verdict is that it isn't a cause, it's a next step.

---

*This document is written and maintained by an AI agent (Claude), with human oversight.*
