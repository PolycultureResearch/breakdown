# The MCP server

The server exposes the engine to AI assistants over
[MCP](https://modelcontextprotocol.io) at `http://127.0.0.1:9090/mcp`
(streamable HTTP; started automatically by `serve`). A chat assistant
connected to it can answer "why was revenue down last week?" by running a real
RCA — Shapley attributions, credible intervals, the honest `unexplained`
remainder — instead of guessing, and "what if we raise marketing spend 10%?"
with a posterior from the what-if engine.

## The six tools

| Tool | Backed by | Description |
|------|-----------|-------------|
| `list_trees` | `/trees` | Every tree this server holds, with its load state (and goal, where one is declared) — for a question aimed at one part of the business rather than the whole |
| `get_tree` | `/meta` + `/dag` | Metric DAG, grains, kinds, declared dimensions, and the loaded data window — assistants call this first |
| `explain_metric` | `/metrics/{name}` | One metric's definition, neighbors, recent series, and fit status |
| `run_rca` | `/rca/{name}` | Full root-cause analysis between two windows |
| `slice_metric` | `/rca/{name}/slices` | Localize a metric's gap within a declared dimension (geo, plan, app version) — the traverse-then-slice follow-up to `run_rca` |
| `run_whatif` | `/simulate` | Do-operator what-if scenario with posterior deltas |

Every tool takes an optional `tree` argument naming which tree to work in
(omit it for the default tree), so an assistant can call `list_trees`, find the
tree that models the area in question, and stay in it. `report_url` carries
`#tree=` so the link keeps naming that tree.

## What the responses carry

Analysis responses are compacted for token economy (rounded floats,
decompositions dropped) and carry two extra fields: `how_to_read` — the
interpretation rules from [docs/model.md](model.md) (what `unexplained` means,
why `share_of_gap` can exceed 100%, ADVI vs NUTS), so the narrating model
states caveats instead of flattening them — and `report_url`, a deep link that
replays the exact analysis in the UI (the engine is seeded, so the link
reproduces the numbers).

Warnings are kept, not compacted away: a fit whose learned direction
contradicts the tree's declared `expected_signs` arrives with its
`sign_warnings`, a mostly-zero series with its `likelihood_warnings`, a
withheld interval with its `ci_status`. The worked session below leans on
exactly this.

## Connecting

From Claude Code:

```bash
claude mcp add --transport http breakdown http://127.0.0.1:9090/mcp
```

or from Claude Desktop via `claude_desktop_config.json` (stdio bridge):

```json
{
  "mcpServers": {
    "breakdown": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://127.0.0.1:9090/mcp"]
    }
  }
}
```

## A worked session

An exchange of this shape, against the [White Cube demo
instance](https://white-cube-demo.fly.dev/ui/) — a synthetic B2C subscription
business whose every anomaly was planted on purpose, so every conclusion below
is checkable against ground truth. **The figures are executed, not
remembered:** every number comes from real tool calls over the MCP wire
protocol against the committed demo snapshots, and
[`tests/test_docs_examples.py`](https://github.com/PolycultureResearch/breakdown/blob/main/tests/test_docs_examples.py)
re-runs the session on every CI run and fails if any of them drifts. The
narration is written to show how the `how_to_read` block and the payload's own
warnings are meant to be *used* — it illustrates a good answer rather than
reproducing a captured one.

The session also runs head-on into **two known issues**, tracked as roadmap
2.21 and 3.7, and reads them the way a careful narrator should — which is the
reason this example exists: the interesting part of connecting an assistant to
an analysis engine is not the happy path but what the assistant does when the
payload contains a verdict that should not be taken at face value, and says
so itself.

**Prompt:** *"White Cube's churn rate jumped this spring. Is it our members
disengaging — and which customers is it?"*

The assistant calls `get_tree`, then `run_rca` on `customer_churn_rate` over
adjacent four-week windows around the jump (reference 2026-03-16 → 2026-04-12,
analysis 2026-05-11 → 2026-06-07), then `slice_metric` three times:
`churned_mrr` by `plan`, `customer_churn_rate` by `country`, and `churned_mrr`
by `country`.

> Churn did jump: **0.91% → 1.23%** of active subscriptions cancelling per
> week, **+34.2%** between those windows. Three findings, one of them a
> verdict I'm deliberately not taking at face value.
>
> **First: it is not disengagement.** The tree models exactly one learned
> driver of churn — `member_activity_rate`, "disengaged members churn" — and
> the analysis *clears* it. Member activity barely moved (95.8% → 96.2% of
> member-days active), its contribution is **2.9%** of the gap with an
> interval crossing zero and `prob_same_direction` 0.65, and **96% of the gap
> lands in `unexplained`** (status `measured`). For a node with one modeled
> parent, that remainder *is* the finding: whatever spiked churn did not come
> through engagement. The payload also carries a `sign_warnings` entry — the
> fitted coefficient on this edge currently contradicts the tree's declared
> direction — which is a second, independent reason not to lean on this edge
> in either direction. (Two things feed that warning: the activity series
> sits near saturation, which the roadmap tracks as issue 3.7, and the fit
> window largely predates the spike.)
>
> **Second: it is a pricing tier.** Slicing `churned_mrr` by plan,
> **professional carries 100.6%** of the lost-MRR gap on a **44.0%** baseline
> share — `prob_concentrated` 0.998, excess CI well clear of zero — while
> studio is flat. That is a professional-tier story: pricing, packaging, or a
> competitor aimed at the top tier.
>
> **Third: it is not a geography — despite one verdict saying otherwise.**
> Slicing the churn *rate* by country returns `localized: true`, but the row
> that clears the 25% threshold is **`__other__`** — the roll-up of countries
> the tree doesn't enumerate — at **26.4%** excess, while no named country
> clears the bar (the largest, BR, carries 13%). A "localized" verdict
> headlined by the leftovers bucket is a known issue (roadmap 2.21): the
> honest reading is *the long tail moved*, not *we found the country*, and
> there is no action that follows from a bucket. The cross-check agrees:
> slicing churned *dollars* by country comes back **not localized**, seven of
> nine rows noise-flagged. A tier, not a geography.
>
> So: the churn spike is concentrated in the professional tier, it is not an
> engagement story, and no geography is implicated. I'd go look at what
> changed for professional-tier subscribers in late April — price, packaging,
> a competitor — rather than at retention campaigns or any one market.
>
> Interactive report: `https://white-cube-demo.fly.dev/ui/#tree=white_cube_tree&rca=customer_churn_rate&reference_start=2026-03-16&reference_end=2026-04-12&analysis_start=2026-05-11&analysis_end=2026-06-07`

Every figure there comes from a field in a tool response, and the hedges are
the payload's own, applied where each one bites. The large `unexplained` is
read as information rather than failure — `how_to_read` says a remainder on a
learned node is "the tree cannot explain this from its declared parents," and
on a one-parent node that is a conclusion, not an apology. The `sign_warnings`
entry is surfaced instead of skipped, and it *changes the answer*: without it,
"engagement cleared" would rest on one fit; with it, the narrator declines to
lean on the edge in either direction and lets the unexplained share carry the
argument. The `__other__` verdict is the centerpiece: the engine's `localized`
flag is technically true and the narration says so — then reads the slice
table underneath it, notices the headline row is the roll-up bucket, names the
known issue, and runs the dollars-side cross-check before concluding. An
assistant that repeated the flag without reading the table would have reported
a geography finding that is not there. That failure mode — a true field,
narrated into a false claim — is what `how_to_read` exists to prevent, and it
is why the response keeps the full slice table rather than only the verdict.

Two of the caveats above are *tracked issues*, not permanent behavior. When
roadmap 2.21 ships, the roll-up bucket will stop headlining a `localized`
verdict and this page's third finding must be rewritten; the test suite pins
the current behavior precisely so that shipping the fix turns this section
red instead of letting it drift. Roadmap 3.7 tracks why the demo's
`member_activity_rate` reads near saturation.

## Securing it

`/mcp` runs whole analyses, so exposing it off loopback without a gate hands
anyone who finds the URL your tree and its data. Set `BREAKDOWN_API_TOKEN` and
`/mcp` requires `Authorization: Bearer <token>` — that one variable gates this
endpoint and nothing else, which is the case it was built for. See
[Authentication](deploying.md#authentication) for gating the rest of the API
too.

## Notes

The first `run_rca`/`run_whatif` on a tree fits models on demand (ADVI) and
can take a minute; fits are cached and shared with the UI. The cache resets
when `--reload` restarts the process. Set `BREAKDOWN_PUBLIC_URL` if the server
is reached at anything other than `http://127.0.0.1:<port>` so `report_url`
links resolve.

---

*This document is written and maintained by an AI agent (Claude), with human oversight.*
