# breakdown

**An open engine for metrics tree construction and analysis**

Metrics trees model causal relationships between business metrics and assist in diagnosing the root causes of changes in metrics. Breakdown models your business metrics as a causal graph and uses Bayesian inference to learn the probabilistic relationships between them. Instead of asking "did revenue go up?", you can ask "which upstream metric caused it, and how confident are we?"

Breakdown handles both determinsistic and probabalisitc causal relationships. Imagine, for example, a KPI like `Net Revenue`, which might be difined as `Gross Revenue - Cost of Goods Sold`. In a metric tree, `Gross Revenue` and `Cost of Goods Sold` deterministically (aka arithmatically) related to `Net Revenue`: The outcome (`Net Revenue`) can be deturmined precisely by the parent metrics using aritmatic. If you notice an unexpect decline in `Net Revenue`, it makes sense to decompose that metric into it's component parts, and see whether the change is driven by an increase in `Cost of Goods Sold` or a decrease in `Gross Revenue` or both. It also lets you define metrics upstream and their relationshps to `Cost of Goods Sold` and `Gross Revenue`, and parent metrics of those, and so on. Breakdown builds a directed acyclic graph of that metrics tree. When you need to find the root cause of a change in a KPI, Because you already have these relationships defined, Breakdown can help find the answer quickly.

Breakdown also handles probabilistic relationships — cases where a metric has a causal effect, but one doesn't determine the other by formula. Consider `Support Ticket Volume` and `Churn Rate`. There's no arithmetic that connects them: you can't compute churn from ticket volume. But historically, when support tickets spike, churn tends to follow a few weeks later, and we can imagine the how filing more support tickets indicates frustration and frustration leads to churn. That's a learned correlation that can be derived probabalistically from historical data. If Churn Rate rises unexpectedly, Breakdown can surface correlated metrics — like Support Ticket Volume, Feature Adoption Rate, or Days Since Last Login — that statistically covaried with churn in the past. You still get the decomposition insight ("churn spiked and ticket volume was elevated two weeks prior"), without pretending the relationship is more certain than it is.

## The causal inference engine 

Most existing metrics tree software ether limits a metrics tree to deterministic relationships, or uses simple correlation coeficients to identify metrics that "move together", but only implicitly point to causal relationships. Without causal modeling, metrics trees exell at visualizing business concepts, but have limited use in diagnosing root causes or identifying opportunities. Breakdown tries to overcome these limitations by modeling probabalisitic relationships between metrics using a causal inference technique. 

## How it works

You define your metric tree in a YAML file:

```yaml
provider:
  type: mock  # or: local, cloud (dbt Semantic Layer)

metrics:
  - name: daily_sessions
    source: jaffle_shop.metrics.sessions

  - name: order_count # a probabalistic relationship 
    source: jaffle_shop.metrics.order_count
    parents:
      - daily_sessions
    priors:
      coefficient:
        distribution: "Normal"
        params: { mu: 0.1, sigma: 0.02 }  # ~10% session→order conversion

  - name: revenue # an arithmatic (deterministic) relationship
    source: jaffle_shop.metrics.revenue
    parents:
      - order_count
      - average_order_value
    seasonality:
      - period: 7
        name: weekly
```

breakdown parses this into a DAG, fetches time-series data for each metric, and fits a Bayesian Structural Time Series model at each node. Each model decomposes a metric into:

- **Trend** — a Gaussian random walk capturing drift over time
- **Seasonality** — Fourier components for any periodic patterns you specify
- **Causal regression** — coefficients on parent metrics, with your priors applied

## Quickstart

**Requirements:** Python 3.14+, [uv](https://github.com/astral-sh/uv)

```bash
git clone https://github.com/your-org/breakdown
cd breakdown
uv sync
uv run uvicorn breakdown.api.main:app --reload
```

Open `http://localhost:8000/ui` to explore the metric tree.

To run a Bayesian analysis on a metric:

```bash
curl -X POST http://localhost:8000/analyze/order_count
```

Then click the node in the UI to see the posterior summary.

## Data providers

| Provider | Config | Status |
|----------|--------|--------|
| `mock` | none | Ready — correlated synthetic data for the jaffle-shop example |
| `local` | `project_path` | Planned — dbt-metricflow against a local project |
| `cloud` | `environment_id`, `host`, `token` | Planned — dbt Semantic Layer API |

Set the provider in your YAML:

```yaml
provider:
  type: cloud
  environment_id: "12345"
  host: "semantic-layer.cloud.getdbt.com"
  token: "your-token"
```

## API

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/dag` | Returns the full metric DAG (nodes + edges) |
| `GET` | `/metrics/{name}` | Returns definition, time series, and posterior summary for a metric |
| `POST` | `/analyze/{name}` | Runs Bayesian sampling for a metric |
| `GET` | `/ui` | Interactive DAG visualization |

## Project structure

```
breakdown/
  parser.py          # YAML parsing and DAG construction (NetworkX)
  data_fetch.py      # Data fetcher interface + Mock/Local/Cloud implementations
  engine/
    model.py         # PyMC model builder (trend + seasonality + regression)
  api/
    main.py          # FastAPI app
static/
  index.html         # Cytoscape.js DAG visualization
examples/
  jaffle_shop_tree.yml
tests/
```

## Running tests

```bash
uv run pytest tests/ -v
```

## Tech stack

- **Statistical engine:** [PyMC](https://www.pymc.io/) (Bayesian inference via MCMC)
- **Data:** [dbt Semantic Layer](https://docs.getdbt.com/docs/use-dbt-semantic-layer/dbt-sl) / [MetricFlow](https://docs.getdbt.com/docs/build/about-metricflow)
- **API:** [FastAPI](https://fastapi.tiangolo.com/)
- **Graph:** [NetworkX](https://networkx.org/)
- **Visualization:** [Cytoscape.js](https://js.cytoscape.org/)

## References

- Levchuk, P. (2025). [The Metric Tree Trap: How math obscures more than it reveals](https://medium.com/@paul.levchuk/the-metric-tree-trap-4280405fd35e). Medium.
- Brodersen, K. H., Gallusser, F., Koehler, J., Remy, N., & Scott, S. L. (2015). [Inferring causal impact using Bayesian structural time-series models](https://projecteuclid.org/journalArticle/Download?urlId=10.1214%2F14-AOAS788). *The Annals of Applied Statistics*, 9(1), 247-274.

## Use case: Solving for "what happened over the weekend?" 
You get a slack message late Sunday evening from the CFO. "I hate to do this again, but can you meet later? Conversions are way down over the weekend. I have to come into Monday's meeting with some idea of what happened." 

You log into the snowflake terminal or whatever, and open the zoom call with the CFO. You write a query to confirm that indeed, conversions are way down starting on friday. What then insues is a rapid series of ad-hock hypotheses and checks; the CFO posing questions (maybe it's just the latest iOS update? is it isolated to users in the United States? Is it caused by a decrease in trial starts or a decrease in the rate of trial to paid conversions?), you sweating and writing sql into the terminal. Hours later, through trial and error, you can define the scope of the problem: what kinds of users, divices, geographcial regions, software versions, etc seem to be behind the observed change. And you have some idea about where in the user experience the problem may reside: was it the numerator or denominator of the rate that changed? Was there less traffic overall or were the people who visited less likely to convert? 

It's been my least favorite part of my job at several companies. But there's something to learn from this kind of late-night triage. Essentially, you and the CFO are using your combined knowledge of the business to generate hypotheses, and checking them. You are traversing the causal graph in your heads to look for abnormalies upstream of the observed change that might explain it. You're essentially doing three things: 
1. Constructing the causal graph upstream of the metric where you observed the abnormal change.  What could have caused it? What do we measure about what could have caused it? And do we observe significant changes in those upstream metrics?
2. Slicing the metrics into smaller and smaller slices to locate the anomoly. That's the process of grouping by operating system, geographical region, user type, software version, etc to try to understand if one or more group of users was driving the overall trend. You might slice up the metric of concern itself (the conversion rate, in this example), or the upstream metrics (the number of trials, the number of conversions, etc).
3. Traversing the causal graph to see if something that could pheasably have caused the changed also changed in the same timeframe. Upstream of conversion rate lies the number of users who could potentially convert, and the number who did convert. Upstream of that are the number of trial starts, and upstream of that are the number of web visitors, and upstream of that are the reach of marketing campaigns, etc. There are also metrics that are known or expected to influence conversion, like the rate of adoption of key features during the trial period. We're moving up the causal chain to look for anomolies that might explain the one we observed.

This process is painful, and it's limited in it's ability to produce insights, but it's not crazy. It probably leaves you ready to present something in the Monday mornign meeting. Can we autmate it? And can we improve on it? 

The premise of breakdown is that by defining the causal graph explicitly, before there is an issue to investigate, we enable a less painfull and more powerful process of root cause analysis. We call that causal graph a metrics tree. The nodes of the graph are metrics. The edges (or connections) are causal relationships, either simple deterministic ones (e.g., `new subscription purchases` / `trial ends` = `conversion rate`) or probabalistic ones (e.g., trying out our key features during the trial period increses the probabily of converting at the end of the trial period). You define those metrics and the relationships between them with the stakeholders, much like you define metrics. You define them in YAML. When you visualize them, they look kind of like a tree. 

Defining the metric tree *a priori* solves two big problems, both related to the reducing the search space when you go looking for the root cause. Consider the two implicit strategies on your late night call with the CFO: slicing the metrics and searching the upstream metrics. You probably combine those strategies, slicing the concerning metric many ways, then slicking all the upstream metrics several ways. Without a metric tree, you could try naively lookihg at all your metrics, and seeing what else changed last friday, and slicing all of those, looking for changes that correlate with your concerning metric in time. Maybe you calculate a correlation cofficinet between your concerning metric and every other metric and each of it's possible slices. Problem one is that the more metrics you examine, the more spurious correlations you are likely to observe. You'll spend your time chasing correlations and then trying to assess causation. Problem two is that the combinatorial explosion of metrics and all their possible slices can start to be computationally intensive. It's not a time you want to be slow. And a third problem is that you may miss any complex, conditional relationships between metrics. 

A metric tree dramatically reduces the search space when you slice up metrics to try to locate the anomoly, and it constrains the space to the metrics that could pheasably cause the observed change. 


