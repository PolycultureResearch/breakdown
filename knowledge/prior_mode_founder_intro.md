# Working out your pricing before you have customers

You're deciding how to monetize — maybe a flat subscription, maybe usage-based pricing, maybe a free tier that converts. The standard advice is "just pick one and iterate," and it's not wrong. But you can do better than picking blind, even with zero revenue and zero users. Here's how.

## Your beliefs are data

You already know things about your business. Not precisely — but you'd bet on ranges. "Of people who hit the site, somewhere between 1 in 20 and 1 in 100 will sign up for a trial." "We could probably charge $30 to $80 a month before the pitch gets hard." "Monthly churn for a tool like ours is probably 3 to 10 percent." Every founder carries a mental model made of exactly these statements.

The problem with keeping that model in your head (or a spreadsheet) is that the pieces don't compose honestly. A spreadsheet forces you to type one number in each cell, so every downstream figure inherits false precision — the classic hockey-stick chart that everyone in the room quietly disbelieves. What you actually believe is a *range* at every step, and the ranges compound.

## What we do instead

We draw your business as a tree of metrics: revenue at the top, and underneath it the things that drive it — subscribers, price, signups, conversion, churn — each connected to what drives *it*. Where a relationship is arithmetic (revenue = subscribers × price), the tree knows it exactly. Where it's a belief (each 1,000 site visits produce 10–50 trials), you state it as a range, and the range is respected.

Then we simulate. The engine plays out thousands of versions of your business, each one drawing a plausible value from every range you stated, and propagates them through the tree together. Out comes not one number but an honest distribution: "under the usage-pricing tree, month-12 MRR lands between $8k and $60k, most likely around $22k."

That sounds wide because it *is* wide — that's the truth about a company that doesn't exist yet, and it's the same truth hiding inside the spreadsheet's single confident number. The value isn't the point estimate. It's what you learn from comparing scenarios:

- **Which option is structurally stronger.** Build a tree per pricing model and compare the distributions, not just the midpoints. One model may have a better median but a much fatter downside.
- **Which belief actually matters.** The simulation attributes every outcome back to the assumptions that drove it. Often two of your fifteen assumptions control most of the spread — those two are what your first experiments should measure. This is how you decide what to test *before* you can A/B test anything.
- **Where a plan quietly depends on something implausible.** If hitting your target requires a conversion rate no one in your category has achieved, the model surfaces that as the load-bearing assumption it is.

## The first customer starts the update

This isn't a throwaway planning exercise. The ranges you stated are, formally, Bayesian priors — and the same model that simulates from beliefs alone starts learning the moment data exists. Your first weeks of real signups begin tightening the conversion estimate; your first churned customers begin tightening churn. The intervals narrow, belief by belief, and the model shifts smoothly from "what we think" to "what we've measured" — without being rebuilt. Watching which intervals shrink and which beliefs get contradicted *is* the early learning of the company, made visible.

The minimum sample size for this kind of analysis is zero. You don't need data to start being rigorous about your own assumptions — and being rigorous about them is most valuable exactly now, while changing course is still cheap.
