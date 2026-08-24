# The MCP server

The server exposes the engine to AI assistants over
[MCP](https://modelcontextprotocol.io) at `http://127.0.0.1:9090/mcp`
(streamable HTTP, started automatically by `serve`). An assistant connected to
it answers "why was revenue down last week?" by running a real RCA, with
Shapley attributions, credible intervals, and the honest `unexplained`
remainder, instead of guessing. "What if we raise marketing spend 10%?" runs
through the what-if engine and comes back with a posterior.

## The six tools

| Tool | Backed by | Description |
|------|-----------|-------------|
| `list_trees` | `/trees` | Every tree this server holds, with its load state and goal where one is declared. For a question aimed at one part of the business rather than the whole |
| `get_tree` | `/meta` + `/dag` | Metric DAG, grains, kinds, declared dimensions, and the loaded data window. Assistants call this first |
| `explain_metric` | `/metrics/{name}` | One metric's definition, neighbors, recent series, and fit status |
| `run_rca` | `/rca/{name}` | Full root-cause analysis between two windows |
| `slice_metric` | `/rca/{name}/slices` | Localize a metric's gap within a declared dimension (geo, plan, app version). The follow-up to `run_rca`: the tree says which metric, the slice says which segment |
| `run_whatif` | `/simulate` | Do-operator what-if scenario with posterior deltas |

Every tool takes an optional `tree` argument naming which tree to work in
(omit it for the default tree), so an assistant can call `list_trees`, find
the tree that models the area in question, and stay in it. `report_url`
carries `#tree=` so the link keeps naming that tree.

## What the responses carry

Analysis responses are compacted for token economy (rounded floats,
decompositions dropped) and carry two extra fields. `how_to_read` holds the
interpretation rules from [docs/model.md](model.md): what `unexplained` means,
why `share_of_gap` can exceed 100%, ADVI vs NUTS. It exists so the narrating
model states caveats instead of flattening them. `report_url` is a deep link
that replays the exact analysis in the UI; the engine is seeded, so the link
reproduces the numbers.

Warnings survive compaction. A fit whose learned direction contradicts the
tree's declared `expected_signs` arrives with its `sign_warnings`, a
mostly-zero series with its `likelihood_warnings`, a withheld interval with
its `ci_status`.

### When a tool refuses

A refusal is part of the contract, not a dead end. Ask for a metric that
isn't in the tree, a dimension it doesn't declare, a window outside the
loaded data, or an RCA on a tree with no data provider, and the tool comes
back with `isError` and a message naming the offending value and — where
there is one — the remedy. Against the bundled example trees — the first from
the mock-provider tree, the second from the cold-start one:

```
Error executing tool explain_metric: Metric 'reveneu' not found.
Known metrics: average_order_value, daily_sessions, order_count, revenue

Error executing tool run_rca: This tree declares no data provider (cold start
mode); this tool needs time-series data. run_whatif works — it simulates over
the tree's declared beliefs.
```

That is written for an assistant to act on: correct the name, switch tools,
move the window. An error is only useful to a model if it says what to do
next, so the tools raise these as *anticipated* failures, which is what makes
the SDK hand the text to the caller rather than keeping it in the server log.
Genuine bugs are the other case and stay deliberately terse — `Error
executing tool run_rca` with the traceback in the server's log, because there
is nothing there for an assistant to fix.

## Connecting

Four paths, ordered by how little the person connecting should have to know.

**Someone deployed breakdown for you (the no-setup path).** Add it as a
custom connector: in Claude, Settings → Connectors → **Add custom
connector**, paste the server's URL (`https://your-host/mcp`), done. No file
edits, nothing installed. Two constraints, because connectors run from
Anthropic's cloud rather than your machine: the server must be reachable on
the public internet (this path cannot reach `127.0.0.1`), and if it sets
`BREAKDOWN_API_TOKEN`, the token has to be entered as a request header when
the connector is added. On Team and Enterprise plans an org admin does that
once (`static_headers`, in beta as of August 2026: header name
`Authorization`, value `Bearer <token>`) and everyone else just clicks
Connect.

**Claude Desktop, when the connector path doesn't fit.** For a server on
localhost, or a token on a plan with no org admin to hold it, install the
extension: download `breakdown.mcpb` from the [latest
release](https://github.com/PolycultureResearch/breakdown/releases/latest),
double-click it, and fill in the two fields it asks for (server URL, and the
token if there is one). Claude Desktop runs it with its own built-in Node;
nothing else gets installed. The bundle lives in
[`packaging/mcpb/`](../packaging/mcpb/).

**Claude Code.** One line, plus the header if the server is gated:

```bash
claude mcp add --transport http breakdown http://127.0.0.1:9090/mcp
# gated server:
claude mcp add --transport http breakdown https://your-host/mcp \
  --header "Authorization: Bearer <token>"
```

**Any other MCP client, by hand.** The stdio bridge the extension automates,
in `claude_desktop_config.json` form (requires Node on the machine):

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
instance](https://white-cube-demo.fly.dev/ui/): a synthetic B2C subscription
business whose anomalies were planted on purpose, so the answer below is
checkable against ground truth. The figures are executed, not remembered.
Every number comes from real tool calls over the MCP wire protocol against
the committed demo snapshots, and
[`tests/test_docs_examples.py`](https://github.com/PolycultureResearch/breakdown/blob/main/tests/test_docs_examples.py)
re-runs the session on every CI run and fails if any of them drifts. The
narration is written prose. It shows how the payload is meant to be read, not
a captured model response.

**Prompt:** *"We shipped an onboarding revamp on August 4, 2025. Did it work,
and can you tell why?"*

The assistant calls `get_tree`, then `run_rca` on `new_mrr` over adjacent
four-week windows around the ship date (reference 2025-07-07 to 2025-08-03,
analysis 2025-08-11 to 2025-09-07), then `explain_metric` on the driver the
RCA names.

> It worked, and the tree can say why, which is the rarer half.
>
> New MRR rose from $1,057 to $1,332 per week, up 26.0%, in the four weeks
> after the ship date against the four weeks before. `unexplained` on that
> node is zero to thirteen decimal places. `new_mrr = new_subscriptions ×
> new_arpu` is an identity, the decomposition is exact, and the count did the
> work: new subscriptions are up 31.1%. Their own identity reports every door
> into a paid plan, including the one that pulled the other way. Trial
> conversions carry 114.5% of the gain, measured over a window shifted back
> one week because the seven-day trial is modeled as a lag. Reactivations
> fell and carry -21.7%. Direct conversions carry 7.2%.
>
> Volume or quality: trials started rose 15.5%, so the business is growing
> underneath, but conversion did more of the work. On `trial_conversions`,
> the conversion rate carries 69.7% of the gap against volume's 30.3%. The
> rate itself jumped from 20.4% to 28.6% of each weekly cohort, up 40.2%.
>
> Why: the conversion rate has two learned drivers, the ones a subscription
> team argues about in every retro. Did trialists activate (upload their
> first work), and how many days did they use the trial. Both moved.
> Activation went from 53.5% to 72.7% of each cohort, up 35.9%; days active
> from 1.31 to 2.32, up 76.1%. The fitted attribution hands 70.7% of the
> conversion gap to activation, with an interval clear of zero (roughly 0.03
> to 0.10 points of conversion) and P(direction) 0.998. In the tree-wide
> ranking, activation outranks trial volume itself.
>
> Now the part worth reading out loud. The second driver, days active,
> carries 36.0% with an interval that straddles zero and P(direction) 0.92.
> The two measures move together, since an activated trialist is an active
> one, so the data pins their combined effect much harder than the split
> between them. The tool says exactly that instead of manufacturing
> precision: sure of the sum, honest about the split.
>
> Context from `explain_metric`: activation averaged 55.5% over the loaded
> 112 weeks, ranging 41.9% to 77.3%. The post-ship window sits at 72.7%,
> near the top of the whole series. This was not a normal month.
>
> Two things this analysis cannot tell you. It cannot say the *revamp*
> caused the activation lift: the tree starts at activation, so anything
> upstream of it, the revamp included, is outside the model. What it
> establishes is the chain from activation through conversion to MRR, with
> the uncertainty stated at each hop. And the windows are deliberately
> adjacent: White Cube is growing fast, and a reference pushed further back
> would fold that growth into the comparison and flatter the revamp.
>
> Interactive report: `https://white-cube-demo.fly.dev/ui/#tree=white_cube_tree&rca=new_mrr&reference_start=2025-07-07&reference_end=2025-08-03&analysis_start=2025-08-11&analysis_end=2025-09-07`

Every figure comes from a field in a tool response, and the hedges are the
payload's own, applied where each one bites. The line between exact and
fitted is the payload's line: `unexplained` reads 0.0 on the two identity
nodes and small-but-`measured` on the fitted conversion node (about 7% of
that node's own gap), so the narration calls the MRR split arithmetic and the
activation split an estimate. The days-active hedge follows the interval, and
the interval is wide because the tree declared two drivers that ride the same
underlying engagement. `docs/model.md` warns that a split between collinear
parents is softer than their sum, and the narration repeats the warning where
it applies instead of quoting both shares with equal confidence. The closing
caveat is `how_to_read`'s first rule, that the tree is the analyst's causal
hypothesis, stated at the top of the causal chain where it actually bites,
rather than appended as a disclaimer. The link replays the analysis in the
live demo; the engine is seeded, so the numbers match.

### More prompts worth trying

Two more sessions we ran while writing this page, left unpinned but worth
reproducing by hand:

- *"Churn jumped this spring. Is it members disengaging, and which customers
  is it?"* The RCA on `customer_churn_rate` clears the engagement theory
  (a −4.2% contribution: member activity moved *up* that spring, so under the
  edge's declared negative sign it pulls against the gap rather than
  explaining it, and the interval crosses zero) and leaves 102% of the gap in
  `unexplained` — over 100% precisely because the one parent pulls the other
  way. On a one-parent node that is the finding, not a failure. The plan
  slice names the professional tier. The country slice on
  the rate returns `localization: "long_tail"` — the concentration sits in
  the `__other__` roll-up rather than in any named country, so the answer is
  "the tail moved; raise this dimension's `top_k` to see inside it", never
  a country. Cross-check the dollars-side country slice, which declines to
  localize at all, before concluding anything about geography.
- *"We can fund one thing next quarter: a retention push we believe cuts
  churn 20%, or 30% more marketing spend. Which is worth more?"*
  `run_whatif` prices the spend lever higher (+$345 against +$161 per week)
  but attaches eight extrapolation warnings, because spend and everything
  downstream of it would sit above anything in the loaded history, while the
  churn lever's number is exact arithmetic conditional on the assumed cut.
  Neither tool call tells you whether the retention push can actually
  deliver its 20%. That assumption is yours, and the response says so.

## Securing it

`/mcp` runs whole analyses, so exposing it off loopback without a gate hands
anyone who finds the URL your tree and its data. Set `BREAKDOWN_API_TOKEN`
and `/mcp` requires `Authorization: Bearer <token>`. That one variable gates
this endpoint and nothing else, which is the case it was built for. See
[Authentication](deploying.md#authentication) for gating the rest of the API
too.

## Notes

The first `run_rca`/`run_whatif` on a tree fits models on demand (ADVI) and
can take a minute; fits are cached and shared with the UI. The cache resets
when `--reload` restarts the process. Set `BREAKDOWN_PUBLIC_URL` if the
server is reached at anything other than `http://127.0.0.1:<port>` so
`report_url` links resolve.

---

*This document is written and maintained by an AI agent (Claude), with human oversight.*
