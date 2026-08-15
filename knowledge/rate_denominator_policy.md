# Should a rate be *required* to declare its denominator?

**Written:** 2026-08-15 · **Status:** decided and shipped (2026-08-15, roadmap
[1.12](roadmap.md#horizon-1--prove-it-a-trustworthy-reproducible-rca)) —
permissive parser, `doctor` fails on an unanswered rate, revisit mandatory at
1.0 · **Decision owner:** the author

Roadmap [1.11](roadmap.md#horizon-1--prove-it-a-trustworthy-reproducible-rca) gave a
rate a node-level `denominator`, so a window's value is `Σnumerator / Σdenominator`
rather than the average of per-period ratios. A follow-up gave it a third state,
`no_denominator: "<reason>"`, for rates that genuinely have none. Every rate in all
four shipped trees now answers.

That raises the question this document answers: **should the parser now refuse a
rate that answers neither?** The row argues it is "materially cheaper before 0.1.0
publishes than after." This is the case for not doing it.

---

## 1. The question changed while we were working

The case for refusing anything in this product is Horizon 0's:
*a plausible wrong number rather than an error*. An undeclared rate used to qualify —
it silently produced the average of per-period ratios, measured **0.02% to 10.9%**
away from the component aggregate on White Cube, with nothing anywhere saying which
arithmetic had run.

That is no longer true. As of the `no_denominator` change an undeclared rate carries
`window_aggregate: "period_mean_undeclared"` in the payload, is named by
`breakdown doctor`, and is captioned in the UI's node card, the Metric tab and the
exported report. Four states are distinguished, including one nobody knew about
(`period_mean_weights_unavailable` — a *declared* denominator whose series does not
cover the window, which used to fall back silently).

By this roadmap's own S-versus-C test, that moves the case from "behavior the docs
describe wrongly" to "a known limitation that is disclosed" — the class principle 3
explicitly permits. **The disclosure work already bought the right to stay
permissive.** Making the field mandatory now pays twice for the same problem.

---

## 2. What the evidence actually says

"43 of 52 rates declared no denominator" reads as proof that optional means never —
and these are *our own* trees, written by the people who care most. It is weaker
evidence than it looks. Of the 35 declared in the sweep:

- **32 came from mechanical derivation** — the tree's own `count = base × rate`
  arithmetic. No human judgment was involved, and the parser could have done it.
- **The residue that needed genuine investigation turned out to have no denominator
  at all.** `time_on_site` is seconds *per session* in a tree with no sessions
  metric. `new_business_opportunity_sales_cycle` averages over opportunities
  *closed*, and the tree counts opened and won but never closed. `page_speed` is a
  **median**, which is not `Σnum/Σden` for any pair of series under any authoring.

So the population is not lazy authors. It is mostly derivable, and the hard residue
is precisely the set where the honest answer is `no_denominator`. Compulsion buys
little of what it appears to buy.

### The main path can be automated, and currently is not

`breakdown/dbt_bridge.py:717` already reads `tp.numerator, tp.denominator` off every
MetricFlow `ratio` metric — it uses them to build a formula edge and then discards
them. White Cube's four ratio metrics each name both sides upstream
(`visit_signup_rate = signups / sessions`, and so on).

**On the governed path — the path the entire product positioning rests on — this
field never needs to be a human chore.** That reframes the decision: it is not
"should we require authors to do work," it is "should we require authors to do work
we could do for them."

---

## 3. Where a mandatory field would bite

**First run.** The tree does not load. Not "loads with warnings" — the server
refuses to start. That is a hard stop before any value, while the
[half-day clock](roadmap.md#product-principles) is running, and it is the exact
failure shape Horizon 2 exists to remove.
[2.9](roadmap.md#horizon-2--make-it-repeatable-a-stranger-can-onboard) already names
it: *"come back once you've built one" is a six-week tax before first value.*

**Iterative authoring.** You cannot look at a partial tree. The project cites
`dbt docs serve` as its spirit; a parser that refuses an unfinished document is the
opposite of that. Authoring a tree *is* an iterative act — the reference tree took
three passes and produced two new roadmap rows on the way.

**Production.** Someone adds a rate to a working tree and the next restart fails to
boot. An authoring omission becomes an outage. Today it warns and labels.

---

## 4. The timing argument prices the wrong cost

"Cheaper before 0.1.0" prices the **migration of existing third-party trees**. There
are none — 0.1.0 is not published — and early adopters will be few for a while. That
cost is near zero today and stays small for months.

The cost that dominates is **per-user onboarding friction**, which is permanent and
*grows* with adoption. Waiting does not make the change more expensive in the way
that argument assumes; shipping it makes every future first run worse, forever.

The pre-1.0 window is a real asset. It is the right argument for a schema change
whose cost is migration. It is the wrong argument for one whose cost is friction.

---

## 5. This was litigated once already, in the other direction

The 2026-08-12 review steelmanned exactly this design — refusing any metric
breakdown cannot fully account for — and concluded it is **worse for adoption**:

> It turns "self-serve breakdown onto your semantic layer" — the thing the client's
> analyst did with no help, which is your strongest signal to date — into a wall of
> refusals on a real dbt project. Your permissive default is why the tool demos well.

Its actual criticism was that the trade-off had been taken **by accident, in one
direction only** — `dbt_sql.py` refused carefully while `dbt_manifest.py` approximated
silently. The lesson from [C15](roadmap.md#horizon-0--correctness-numbers-the-engine-cant-defend)
was never "refuse more." It was "choose the policy deliberately and propagate it."

Choosing permissive-and-disclosed here, on purpose and written down, satisfies that.
Choosing refuse-at-parse would be over-correcting from a review that argued the
opposite — and [2.19](roadmap.md#horizon-2--make-it-repeatable-a-stranger-can-onboard)
is the live reminder of what over-refusal costs: `fill_nulls_with` is refused
correctly and blocks **18 of 26 metrics** in the only real dbt project in reach.

---

## 6. Recommendation

Three moves, not one.

1. **Derive it in the dbt bridge.** The manifest names both sides on every `ratio`
   metric and the bridge already reads them. On the governed path the field fills
   itself, and most of the "authors will not do it" problem evaporates. Highest
   value of the three, and small. Note the adjacent
   [2.19](roadmap.md#horizon-2--make-it-repeatable-a-stranger-can-onboard) finding:
   `_SIMPLE_RATIO` does not recognise `num / nullif(den, 0)`, the canonical safe
   idiom, so the same pass should teach it that shape.
2. **Make `doctor` fail — non-zero exit — on an unanswered rate.** `doctor` is
   already the trust gate, and the roadmap notes that every failure path in the
   product points users at it. That puts enforcement where someone is asking
   *"can I trust this?"* rather than where they are asking *"can I see my tree?"*
3. **Leave the parser permissive.** The label already tells the truth on every
   surface.

Revisit **at 1.0**, when the schema is allowed to break anyway and there is real
usage data.

**Shipped 2026-08-15, as [roadmap 1.12](roadmap.md#horizon-1--prove-it-a-trustworthy-reproducible-rca).**
Move 2 landed as specified. Move 1 turned out to be narrower than framed: for a
MetricFlow `ratio`-type metric, `dbt_bridge.py` already translates numerator and
denominator into a `formula: "num / den"` edge, and `parser.py`'s existing
`check_rate_denominator` already derives `.denominator` from that shape — nothing
was actually being discarded for that metric type, and this was confirmed with an
end-to-end test rather than assumed. The real gap was the `derived` metric type's
equivalent-but-safer idiom, `num / nullif(den, 0)`, which `_accept_formula`
refused outright (no function calls in breakdown's formula grammar), so those
metrics never became formula nodes and never reached the derivation at all — this
is the same fact [2.19](roadmap.md#horizon-2--make-it-repeatable-a-stranger-can-onboard)
found from the other direction. `_translate_derived` now rewrites that one exact
idiom to plain `num / den` before translation; anything else is still refused by
name. Move 3 was a no-op by construction (nothing proposed changing the parser).

### The narrow version, if teeth are wanted sooner

Refuse only a rate that has **undefined periods and no denominator**. That is the
case where the fallback is not merely different but incoherent — the average of
per-period ratios is taken over a shifting basis. It is rare, it explains itself,
and it cannot fire on a tree that worked yesterday unless the data changed shape.
This is a strictly smaller and better-targeted rule than blanket refusal, and it is
what I would ship if permissive feels too loose.

---

## 7. What would change this decision

**A client running an RCA off an undeclared rate and acting on it.** That would mean
the disclosure is not landing — that a labelled fallback reads, to a real reader,
like a measurement. The gate should then move earlier, and the narrow rule in §6 is
where it should move to first.

Worth watching for deliberately rather than assuming either way. The
[`read-the-numbers`](../.claude/skills/read-the-numbers/SKILL.md) practice is the
place to look for it: an undeclared rate on a demo tree should be visible to a
reviewer following that procedure, and if it is not, the labelling is the thing to
fix before the parser.

## 8. Where this is least sure

- **Whether `doctor` is really the gate people run.** The argument in §6 assumes it
  sits in the onboarding path. If most users never run it, moving enforcement there
  moves it nowhere, and the honest response is to make `serve` print its summary at
  startup rather than to fail the parse.
- **Whether the bridge derivation covers enough.** It fixes the dbt path. A
  `warehouse` tree with hand-written `sql:` gets nothing from it, and that is the
  segment [2.2](roadmap.md#horizon-2--make-it-repeatable-a-stranger-can-onboard)
  calls the zero-integration on-ramp. If that becomes the main path, this
  recommendation weakens.
- **The 0.02%–10.9% spread is from one tree.** How wrong the fallback gets on a
  real client tree is unmeasured, and a much larger error would strengthen the case
  for refusing.
