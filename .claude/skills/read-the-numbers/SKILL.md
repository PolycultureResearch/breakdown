---
name: read-the-numbers
description: Run breakdown on its demo trees and interrogate the output before calling a change done. Use after changing attribution, intervals, fitting, a provider, the API payload, or any rendering — it catches silent wrong numbers and misleading presentations, which pass both the test suite and code review by construction.
---

# Read the numbers

Reading code finds mechanisms. Running the product finds which mechanisms fire,
at what magnitude, and how the result reads to a person. This is the second one.

Rationale and the evidence behind each step:
[`knowledge/reading_the_numbers.md`](../../../knowledge/reading_the_numbers.md).
Tree-by-tree specifics, known-good anchors and known-bad nodes:
[`fixtures.md`](fixtures.md) — read it before step 1.

Not a smoke test. `breakdown doctor` and the demo tests already answer *did it
run*. This answers **is the number right, and does it say what it means**.

## 1. Run two trees, chosen to differ

**White Cube** first — planted anomalies with known ground truth. **B2B MRR**
second — 106 metrics, mixed grain, for whether anything structural breaks at
scale. Commands and windows in `fixtures.md`.

One tree is not a sample: a tree exercises the paths its own shape reaches and
silently skips the rest. Pick windows that exercise *the change*, not just the
tour's.

## 2. Read the most prominent number first, and distrust exact values

Start at `ranked_causes[0]` and the target's headline gap — what a user sees
first. Then scan for numbers sitting on a boundary. **Exactness is the tell:**
real estimates are not round, so a round one is usually a clamp, a fabrication,
or a structural zero wearing a measurement's clothes.

Treat every one of these as a finding until explained:

- a score of exactly `1.0` or `0.0` (C5 sat at exactly `1.0` on the demo tree's
  top cause through two hostile reviews — a saturated clamp reads as certainty)
- a share of exactly `100%`
- `ci_95` of exactly `[0, 0]`, or a bound sitting exactly on `0`
- `prob_same_direction` of exactly `1.00` — it is a proportion over 500 replicates,
  so its ceiling is 0.998 and there is no representable value between. Explain it
  **once per run** and record how many nodes carry it; do not re-explain each one.
- a value **identical across two different windows** (a horizon-invariant interval, a cached answer)
- a series that is identically zero, or never changes sign

## 3. Add up what must add up

Contributions + `unexplained` = `gap`. Shares sum to 100%, or the excess is
accounted for by parents that offset. Do it by hand once, on the **rendered**
table, not the payload — a row missing from the render is the defect.

## 4. Look at the rendered surface

Open `/ui`. Export the report. A correct payload rendered dishonestly is
indistinguishable, to the reader, from a dishonest payload.

**Highest-yield step in this file. Do it even when short of time.**

**Read the same number on every surface and diff them.** There are at least
five, and they can disagree: the canvas card, the Metric tab's chart, the RCA
table in *both* its Headline and Detailed forms (which re-base shares
differently), the exported HTML, and the raw payload. Several defects are
disagreements *between* surfaces rather than errors within one.

- Does a node the engine declined to analyze look analyzed?
- Does a withheld interval look like a measured one? A `null` like a `0`?
- Do colour and arrow agree with the sign *and* with `direction` — including
  when `direction` is **absent**? An undeclared metric must not paint a
  confident claim.
- Is every caveat in the export? A hover tooltip is **absent** from a static
  report, and the export is what circulates without its author.
- Does a lagged contribution say so? It was measured over a different window
  than the header names.

## 5. Perturb one input

Widen the window by one period. Add a period. Let the reference default instead
of naming it (`reference_defaulted` in the response).

The response must be **monotone and proportionate**. More periods *should*
narrow an interval — the window mean's sd shrinks like 1/√n, and `docs/model.md`
says so. The findings are the other shapes: an interval that **widens** with
more data, a number that **jumps** at one particular length, or a score that
reverses. Windows must stay valid for the grain (whole Monday→Sunday blocks on a
week-grain node), and a bad pick snaps silently rather than erroring — check the
`effective_windows` you got back, not the ones you typed.

## 6. Check what the docs claim about this exact run

Cheap, and it finds as much as anything above. The tour, the README and this
skill's own `fixtures.md` all narrate specific windows. Read what they say about
the window you just ran and check it still holds — shares drift when attribution
changes, and a presenter reading a stale number aloud on a client call is a
worse failure than a wrong pixel.

Where a doc claims a test protects a number, open the test and confirm it
actually pins that number rather than a property around it.

## 7. Perturb the model, not just the window

The What-if tab is the perturbation surface the product actually ships. Move one
driver by a known amount and confirm the named node responds in the right
direction and rough magnitude, and that an absurd intervention is refused rather
than extrapolated.

## Output

State the numbers you looked at. A review that did not run is otherwise
invisible. Date it — a dated observation is a record and cannot go stale, an
undated one drifts into a false claim.

> **2026-08-13** · White Cube `new_mrr`, ref 2026-01-05→2026-02-01, an 2026-02-09→2026-03-08.
> 1701.5 → 1449.6, gap −251.8 (−14.8%), `unexplained` −0.00 (exact identity,
> reconciles). `new_subscriptions` −324.8 share +1.290 ci [−534.8, −76.8]
> psd 0.998; `new_arpu` +73.0 share −0.290 ci [−168.3, 293.1] psd 0.708 —
> shares sum to 1.000, volume established, rate not. `ranked_causes[0]`
> `new_subscriptions` 0.633 via `new_mrr`. Matches Story A. Payload-wide: 4 of 8
> contributions carry `prob_same_direction` exactly 1.00 (resolution ceiling,
> explained once). Rendered table reconciles; export carries the caveats.

"Looks fine" is not an output.

## The ratchet

For every finding, ask: **could an invariant have caught this?** If yes, add it
to `tests/test_project_invariants.py` and say so. The manual step must shrink
over time, not grow — that is what keeps this from becoming ritual.

Anything you can assert, assert. This practice is for what needs a reader.

## Finish

Stop the servers you started. Say which steps you skipped and why — a review
with a gap named is usable; one that implies full coverage it did not have is
not.
