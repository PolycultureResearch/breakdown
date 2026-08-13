# Reading the numbers: the review step breakdown was missing

**Written:** 2026-08-13 · **Status:** proposal, with evidence from the 2026-08-12/13 review cycle

Two hostile reviews swept this codebase — engine, statistics, providers, API, MCP,
packaging, docs — and produced 33 findings between them, most of them real. Both
were conducted by reading. Working those findings down over two days surfaced a
second set of defects that **neither review could have found**, not because the
reviewers were careless but because the instrument was wrong: they were inferring
from artifacts, and the defects only exist once the product runs.

This document argues that a data product needs a named, systematic step that
regular software review does not, proposes what it should contain, and
recommends how to formalize it.

---

## 1. What the evidence actually shows

Split this cycle's findings by the instrument that found them.

**Found by reading** — the classic review, and it works well: a dbt metric's
`filter` silently dropped (C15); a snapshot key ignoring the definition (C16);
duplicate metric names merging into one DAG node (C6); an uncapped O(2ⁿ)
enumeration; an unbounded caller-controlled provider window; a heavy call on the
event loop; `MIN_FIT_PERIODS` unenforced on the default path; the auth surface;
nearly every packaging and documentation defect. All of these are visible in a
diff. A skeptical reader with the file open finds them.

**Found only by running, and looking at the output:**

| Defect | Why reading missed it |
|---|---|
| **C5's real trigger** | Two reviews *and* a triage pass all had the mechanism — `min(abs(share), 1.0)` — and all three framed it as a near-zero-gap edge case. Running the bundled demo showed `ranked_causes[0] = order_count` at **exactly 1.0**, clamped from a true 1.653, over an ordinary fortnight on a gap of +$596 against a $26.4K baseline. The mechanism was known; its *domain* was wrong, in a way that would have shaped the fix wrongly. |
| **H1's actual failure** | Reading predicted a NaN reaching the JSON encoder. Running produced `KeyError: '__import__'` — numpy's divide-by-zero *warning* machinery resolving a name from a restricted-globals `eval`. A different bug needing a different fix, living in an interaction between two libraries and present in no single file. |
| **`BOOT_BLOCK["day"] = 7`** | Nobody suspected it. It fell out of *measuring* the block cap in order to justify a number. A 7-day block holds each weekday exactly once, so a weekly seasonal component cancels identically in every replicate — the shipped default sits at a local **minimum** of honest interval width, ~⅓ of its width at block 3. No amount of reading finds a resonance. |
| **H7** | Every dollar figure in the README's MCP transcript was 28–100× off and its headline conclusion inverted. Visible in one command; invisible in any diff. |
| **The whole UI class** | `applyRcaOverlay` tinted on `node.gap >= 0` — and `null >= 0` is `true` in JavaScript, so every node the engine *declined to analyze* rendered green with an upward arrow. `0 >= 0` did the same for a metric that provably didn't move. The live attribution table omitted `components.trend`, so shares summed to **108.5%** and `unexplained` was understated. A `direction: down` goal missed by 3× filled its progress bar to 100%. `fit_quality` was rendered nowhere in the product. **In every one of these the payload was correct and the rendering lied.** |
| **C18's magnitude** | Reading found the branch. Running showed 19 fabricated zeros and, decisively, that **nothing logged** — which is what moved it from "medium" to Horizon 0. |

The pattern is not that reading is bad. It is that **reading finds mechanisms,
and running finds which mechanisms matter, at what magnitude, and how the result
reads to a person.** For a product whose output is a number someone will act on,
the second question *is* the product.

---

## 2. Why the `grill` skill cannot close this

The `grill` skill is a good adversarial checkpoint and it earned its findings
here. But look at its steps. Mode A asks: restate the request, list assumptions,
find edge cases, find ambiguity, ask the sharpest questions. Mode B asks: what is
this trying to do, where does it break, what is the weakest assumption, what was
traded away, what breaks first, steelman an alternative.

**Every one of those is answerable with the code open and nothing running.** That
is by design — it is a *spec and design* instrument, and it should stay one. Its
own framing gives it away: "point at exact lines, functions, or decisions."
Lines, functions, decisions. Not outputs.

Which is why it found C15 and missed C5's domain, found the unbounded cache and
missed that the top number in the UI was a saturated clamp.

---

## 3. Is it a smoke test?

No, and the distinction matters for what we build.

A **smoke test** asks *did it run* — binary, fast, no domain knowledge required.
breakdown already has these: `test_white_cube_demo.py`, `breakdown doctor`, the
`base-install` and `sdist` CI jobs. They are necessary and they are not what is
missing.

What is missing is closer to what a data team calls a **results review**, and it
splits into three distinguishable questions that are worth naming separately,
because they have different instruments:

1. **Is it arithmetically right?** Do the identities reconcile — contributions
   plus `unexplained` equals the gap, shares sum to 100%, an exact Shapley
   decomposition telescopes? **Mechanizable.** This should be a test, always.
2. **Is it statistically calibrated?** Does a 95% interval cover at 95%? Does
   widening a window widen the interval monotonically? **Mechanizable but
   expensive** — this is what `tests/test_calibration.py` is for, and roadmap
   S5/S17 are the open work on making it able to fail.
3. **Is it legible?** Does the number, *as presented*, mean to a reader what it
   means to the engine? **Not mechanizable.** This is where every UI defect
   above lived, and no test in any suite would have caught one of them.

The gap is (3), plus the parts of (1) and (2) that nobody thought to assert
because nobody had looked at the output and been surprised.

---

## 4. Why breakdown needs this more than most software

Three properties compound:

- **Wrongness is silent by construction.** Horizon 0's own charter is "a
  *plausible wrong number* rather than an error." There is no exception, no 500,
  no red test. The only detector is a person who knows what the number should
  look like.
- **Ground truth is available and underused.** `tests/synthetic.py` and the mock
  provider mean we can *plant* a cause and check it is recovered — an asset most
  data products would kill for. `test_calibration.py` does this and S17 records
  that its coverage test is structurally unable to fail.
- **The fixture is the product.** C11's roadmap row says it outright: "the mock
  is the demo, the tutorial, and most of the test suite." A mock defect is a
  product defect, and the mock has already shipped a $10²⁵ MRR and guaranteed
  spurious sign warnings.

---

## 5. The proposed practice: **read the numbers**

A required step before a change to the engine, a provider, or any presentation
surface is called done. Six moves, each of which caught something real this
cycle.

**1. Run at least two trees, and make them differ.** One small and bundled
(`jaffle_shop_tree.yml`), one wide and real-shaped (`b2b_mrr_tree.yml` or the
White Cube demo). This is not ceremony: C5 fired on the *small* tree, and the
block-length resonance needed the *daily seasonal* one. One tree is not a
sample — a tree exercises the paths its own shape reaches and silently skips
the rest.

**2. Read the most prominent number first, and distrust exact values.** Whatever
the UI puts at the top — `ranked_causes[0]` — and then look for numbers sitting
on a boundary: a score of exactly `1.0`, a share of exactly `100%`, an interval
of exactly `[0, 0]`, a `prob_same_direction` of exactly `1.00`. **Exactness is
the tell.** Real estimates are not round. C5 showed as exactly 1.0; C4's
degeneracy showed as `[0.00, 24.94]` with a lower bound sitting precisely on
zero; the structurally-absent seasonal component showed as `{0.0, [0.0, 0.0]}`.
Every one of those was a clamp or a fabrication wearing a measurement's clothes.

**3. Add up what must add up.** Contributions + `unexplained` = gap. Shares sum
to 100%, or the excess is accounted for by parents that offset. Do the
arithmetic by hand once. This is how the missing `components.trend` row was
found: the rendered shares summed to 108.5% and the 8.4% was a real estimated
component sitting outside the table.

**4. Look at the rendered surface, not the payload.** Open the UI. Open the
exported report. **A correct payload rendered dishonestly is indistinguishable,
to the reader, from a dishonest payload** — and the exported report is read
without its author present, so a caveat that only exists as a hover tooltip is
*absent* from it.

**5. Perturb one input and check the response is sane.** Widen the window by a
day: does the interval move monotonically? Add a period: does anything jump?
This is the move that exposed C4's non-monotonicity (a variance ratio of 0.55
at n=13 and 0.50 at n=14) and the block-length resonance. It is also the
cheapest of the six.

**6. Plant something you know, and check it comes back.** The calibration suite
does this; the point here is to do it *ad hoc* for the change in front of you.
If you changed attribution, plant a step in one parent and confirm that parent
is named.

### The rule that keeps it from becoming ritual

**Anything you can assert, assert in a test. The practice is for what needs a
reader.**

Without that rule the checklist accumulates items that should have been
automated, gets slower, and stops being run — the classic fate of a manual QA
document. Concretely: moves 3 and 5 mostly belong in
`tests/test_project_invariants.py`, which already holds four such rules and
already enforces "no engine result reaches an encoder unsanitized" structurally.
Move 6 belongs in `test_calibration.py`. What genuinely cannot be automated is
moves 1, 2 and 4 — choosing which trees, noticing that a number is suspiciously
round, and judging whether a rendering means what it says.

**Every time this practice finds something, ask whether an invariant could have
caught it, and if so add the invariant.** That is the ratchet: the manual step
shrinks over time instead of growing. C4's fix, for example, produced the
invariant "no published `ci_95` is ever zero-width", which is now a property test
and no longer needs a human.

---

## 6. Recommendation: a sibling skill, not an extension

**Yes, formalize it — as a separate skill beside `grill`, not inside it.**

The two have different trigger points, different instruments and different
outputs, and merging them would blunt both:

| | `grill` | proposed `numbers` |
|---|---|---|
| When | Before implementation, or on a design | After implementation, before "done" |
| Instrument | Reading and inference | Execution and judgement |
| Output | Questions and named assumptions | Observations about specific values |
| Finds | Mechanisms, ambiguity, weak assumptions | Which mechanisms fire, at what size, and how they read |

Concretely, the skill should:

- Take **the change** as its subject, not the codebase, and select trees and
  windows that actually exercise it (a provider change wants a real dbt project;
  an attribution change wants a formula node with offsetting parents).
- Carry the **tells** from §5.2 as an explicit list, because that is the part
  that is hard to remember and easy to teach.
- Require the reviewer to **state the numbers they looked at**, so a review that
  did not actually run is visible as such. "Looks fine" is not an output;
  "`ranked_causes[0] = order_count` at 0.4406 via `revenue`, shares 165%/−62%
  summing to the gap within 3.7%" is.
- End by asking **"could an invariant have caught this?"** for anything found,
  and route it to a test if so.

A standing fixture set is worth having alongside it — a small table of
(tree, window, what this pair is for) so the practice runs on the same ground
each time and drift is visible. `knowledge/demo_guided_tour.md` and
`tests/test_white_cube_demo.py` are the existing precedent.

---

## 7. What I am least sure about

- **Whether two trees is the right number**, or whether the real requirement is
  "one tree per structural shape the engine supports" — mixed grain, formula
  nodes with offsetting parents, a rate over a true-zero denominator, a stock.
  The evidence here says shape matters more than count, but I have one cycle of
  data, not a study.
- **Whether the practice survives contact with a deadline.** The honest failure
  mode is that it gets skipped exactly when it matters most — before a client
  deployment. The ratchet in §5 is the mitigation, but it is untested.
- **Whether "read the numbers" generalizes past this project.** It is stated here
  in breakdown's own vocabulary. Whether the tells transfer to a product whose
  output is not a decomposition with intervals is an open question, and probably
  not worth answering until someone tries.

---

## 8. The one-line version

Two hostile reviews and a triage pass all had C5's mechanism in hand. None of
them ran the demo and looked at the top number. **Reading the code finds
mechanisms; running the product finds which ones matter** — and for a tool whose
entire value proposition is that its numbers can be trusted, the second is not
optional.
