# Research: Per-node natural grain for metric trees

Design research for breakdown's grain question (roadmap 1.7): should metric
trees declare a per-node natural grain, resample before fit/attribution, and
split attribution into a window-aggregate headline plus per-period drill-down?
Findings per topic, then implications. Produced 2026-07-28; the resulting
decisions live in [`grain_design.md`](grain_design.md).

## 1. How dbt MetricFlow / the dbt Semantic Layer handles grain

MetricFlow — the engine breakdown reads from — has converged on exactly the
pattern under consideration: **grain is declared per object, with a default at
query time, and queries can traverse up (coarser) but never below the declared
grain.**

- **Time dimensions declare granularity at the column level**
  (`granularity: day|week|month|quarter|year`, plus sub-daily
  `hour/minute/second` since v1.9). Declaring grain "creates a natural
  hierarchy" — you can query a day-grain dimension at month grain, but never
  finer than declared. ([Dimensions](https://docs.getdbt.com/docs/build/dimensions),
  [sub-daily support PR](https://github.com/dbt-labs/dbt-core/pull/10483),
  [feature issue](https://github.com/dbt-labs/dbt-core/issues/10475))
- **`agg_time_dimension`** is set per semantic model (overridable per measure)
  and names the time series a metric aggregates over; `metric_time` is the
  alias MetricFlow treats identically for cross-metric alignment.
  ([Measures](https://docs.getdbt.com/docs/build/measures),
  [How the Semantic Layer works](https://www.getdbt.com/blog/how-the-dbt-semantic-layer-works))
- **Metric-level `time_granularity` (née `default_granularity`)** was added
  precisely because "metric_time defaults to the smallest available
  granularity, which can be surprising" — an MRR metric can declare
  `time_granularity: month` so a bare query returns month grain, while the
  dimension's finer grain remains queryable. This is direct precedent for
  *node-level grain declaration distinct from the data's storage grain*.
  ([dbt-core PR #10378](https://github.com/dbt-labs/dbt-core/pull/10378),
  [issue #10376](https://github.com/dbt-labs/dbt-core/issues/10376),
  [Creating metrics](https://docs.getdbt.com/docs/build/metrics-overview))
- **Cumulative metrics** make window semantics explicit and require a
  continuous **time spine**: `window` is a sliding window (trailing 1 month),
  `grain_to_date` resets at each grain boundary (MTD). Granularity interaction
  with cumulative metrics has been a long-standing pain point
  ([issue #791](https://github.com/dbt-labs/metricflow/issues/791)).
  ([Cumulative metrics](https://docs.getdbt.com/docs/build/cumulative),
  [Time spine](https://docs.getdbt.com/docs/build/metricflow-time-spine))
- **Derived and ratio metrics** combine *metrics*, not raw columns, so inputs
  are first aggregated to the common requested grain and then combined — i.e.,
  MetricFlow is aggregate-then-combine, never combine-then-aggregate. Ratio
  metrics exist specifically to avoid the sum-of-ratios error. `offset_window`
  on derived metrics handles time-shifted inputs, with known bugs in nested
  cases ([issue #882](https://github.com/dbt-labs/metricflow/issues/882)).
  ([Derived metrics](https://docs.getdbt.com/docs/build/derived))
- **Custom calendars** (fiscal months, retail 4-4-5) are supported via extra
  time-spine columns — grain is not assumed Gregorian.
  ([2024 release notes](https://docs.getdbt.com/docs/dbt-versions/2024-release-notes),
  [custom calendar issue](https://github.com/dbt-labs/metricflow/issues/820))

**Takeaway:** the semantic layer breakdown consumes already models "natural
grain" as a first-class, per-metric property with a coarsen-only rule.
breakdown mirroring this maps 1:1 onto what a MetricFlow-backed provider can
actually serve.

## 2. Mixed-frequency time series modeling

- **MIDAS regression** (Ghysels et al., 2004) is the standard econometric
  answer when a low-frequency target regresses on high-frequency drivers: keep
  the fine-grain regressors and estimate a parsimonious distributed-lag
  weighting instead of aggregating them. Widely used in GDP nowcasting.
  ([Ghysels, "The MIDAS Touch"](https://rady.ucsd.edu/_files/faculty-research/valkanov/midas-touch.pdf),
  [Handbook chapter](https://www.sciencedirect.com/science/article/pii/S0169716119300057),
  [midasr](https://github.com/mpiktas/midasr))
- **State-space / Kalman-filter approaches** are the Bayesian-native
  alternative: model the latent process at the finest grain and treat coarse
  observations as missing/aggregated data. This is the natural extension path
  for a BSTS engine (PyMC handles missing observations), and is standard in
  nowcasting.
  ([Bai, Ghysels & Wright, "State Space Models and MIDAS Regressions"](https://cdr.lib.unc.edu/downloads/pr76fc17f),
  [precision-based mixed-frequency VARs](https://arxiv.org/abs/2112.11315),
  [J. Stat. Software toolbox](https://www.jstatsoft.org/article/download/v104i10/4404))
- **Temporal aggregation bias** is a large, settled literature: aggregation
  changes the stochastic process (Wold representation), distorts MA
  coefficients, can flip signs of cointegrating relationships, and induces or
  destroys Granger causality ("spurious causality"). Silvestrini & Veredas
  (2008) is the canonical survey.
  ([survey, SSRN](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=1290518),
  [J. Econ. Surveys](https://ideas.repec.org/a/bla/jecsur/v22y2008i3p458-497.html),
  [temporal aggregation & monetary policy](https://www.federalreserve.gov/econres/feds/files/2022054r1pap.pdf),
  [review in accounting research](https://www.emerald.com/jal/article/47/5/110/1267416/A-review-of-temporal-aggregation-and-systematic))
- **But the practical consensus is not "always model at the finest grain."**
  The temporal-hierarchies literature (Athanasopoulos et al. 2017; MAPA) shows
  aggregation *loses information* but also *suppresses noise*, and the accuracy
  benefit of coarse levels "increases with modelling uncertainty." A 2025 paper
  names the flip side the **"Granularity Paradox"**: disaggregating below a
  series' natural grain inflates in-sample fit and compounds out-of-sample
  error.
  ([Forecasting with temporal hierarchies](https://www.sciencedirect.com/science/article/abs/pii/S0377221717301911),
  [forecast reconciliation review](https://robjhyndman.com/papers/hf_review.pdf),
  [Rediscovering Bottom-Up](https://arxiv.org/html/2407.02367v1),
  [Granularity Paradox](https://arxiv.org/pdf/2607.05450))

**Takeaway:** fitting a month-grain snapshot metric on daily step-function
data is a textbook instance of the pathology this literature warns about — the
daily "observations" are not 30 independent data points, so daily-grain
posteriors are spuriously tight and daily covariances spurious. Modeling each
relationship *at the coarser of the two metrics' grains* is defensible and
standard; latent fine-grain state-space modeling (MIDAS/Kalman) is the
principled upgrade path, not the MVP.

## 3. Shapley / decomposition of change over time windows

- **The two-factor interaction problem is classical.** For Y=B·C,
  ΔY = ΔB·C₀ + ΔC·B₀ + ΔB·ΔC; Laspeyres-style decompositions leave the ΔB·ΔC
  residual, Paasche assigns it asymmetrically. Sun (1998)'s "jointly created
  and equally distributed" principle splits the interaction evenly — and
  **Ang et al. (2003) proved Sun's method is identical to the Shapley value**;
  its multiplicative counterpart is a modified Fisher ideal index. So
  breakdown's Shapley split of deterministic edges is the Sun/Shapley/Bennet
  family, which is "perfect" (zero residual), symmetric in factors, and
  time-reversal robust.
  ([Ang, Properties and linkages of IDA methods](https://www.sciencedirect.com/science/article/abs/pii/S0301421509004327),
  [Decomposition analysis: when to use which method?](https://www.tandfonline.com/doi/full/10.1080/09535314.2019.1652571),
  [mean-rate-of-change critique of Sun](https://www.sciencedirect.com/science/article/abs/pii/S0306261905000358))
- **Index-number theory is the same math**: Bennet indicators are the additive
  price/quantity decomposition ΔV = Δp·q̄ + Δq·p̄ (endpoint means = two-factor
  Shapley); Fisher indicators are the multiplicative twin; Diewert (2005)
  derived exact decompositions of value change on this basis. Bennet/Fisher
  satisfy time reversal; this is the vocabulary to cite for the window headline
  view.
  ([de Haan, Price and Quantity Indicators](https://www.istat.it/storage/17Meeting-Ottawa/Room-papers/Indicators-and-Index-Numbers.pdf),
  [Bennet indicators](https://www.sciencedirect.com/science/article/abs/pii/S0165176508002796),
  [Konüs–Bennet–Luenberger decompositions](https://www.sciencedirect.com/science/article/pii/S0038012123000733),
  [IMF index number theory ch. 16](https://www.imf.org/external/np/sta/tegeipi/ch16.pdf))
- **Fine-grain-then-sum ≠ decompose-the-aggregates, and the gap is exactly a
  covariance term.** The identity is E[BC] = E[B]E[C] + cov(B,C) ("mean of
  product = product of means + covariance"): the window aggregate of a product
  metric equals the product of window means only when the within-window
  covariance of the factors is zero. Decomposing per-day and summing attributes
  the within-window covariance implicitly; decomposing window aggregates drops
  it unless surfaced explicitly. The demography literature (Kitagawa,
  Das Gupta) handles the analogous problem with symmetric allocation of
  interaction terms across cross-classified factors.
  ([covariance properties](https://data140.org/textbook/content/chapter-13/properties-of-covariance/),
  [law of total covariance](https://en.wikipedia.org/wiki/Law_of_total_covariance),
  [Das Gupta rate decomposition](https://journals.sagepub.com/doi/pdf/10.1177/1536867X1701700213),
  [additive decompositions with interaction effects, IZA](https://docs.iza.org/dp6730.pdf))
- **Known critique of even splits**: de Bruyn/Casler note Sun's 50/50
  interaction split distorts when a large interaction is added to a small main
  effect — an argument for *showing* the interaction/covariance term rather
  than silently burying it.
  ([mean-rate-of-change paper](https://www.sciencedirect.com/science/article/abs/pii/S0306261905000358))

**Takeaway:** the proposed two-level view (window-aggregate Bennet/Shapley
headline **with explicit interaction term** + per-period covariance
drill-down) is exactly what the literature supports: the headline is the
Bennet/Sun decomposition; the drill-down explains the covariance residual that
separates it from the per-period sum.

## 4. How commercial metric-tree / diagnostic tools handle grain

Findings are thinner here (most tools don't document internals), but a
consistent pattern emerges: **tools either force a single analysis grain or
declare grain per metric; none fits relationships below a metric's natural
grain.**

- **Tableau Pulse** is the clearest precedent: a metric definition *requires*
  a time dimension, supports **day/week/month/quarter/year only**, explicitly
  rejects sub-daily data as "not a good fit," and lets you set a **minimum
  time granularity per metric** so users only see sensible periods. Its "Top
  Drivers" runs over a defined comparison period at the metric's grain.
  ([Create metrics with Tableau Pulse](https://help.tableau.com/current/online/en-us/pulse_create_metrics.htm),
  [InterWorks guide](https://interworks.com/blog/2024/03/13/how-to-build-the-best-metrics-in-tableau-pulse/))
- **Power BI**: anomaly detection is line-chart/time-series scoped with
  granularity chosen by the report's date hierarchy (daily/hourly/minutely
  configurable); the decomposition tree drills across *categorical* dimensions
  and doesn't reconcile mixed time grains at all.
  ([Anomaly detection tutorial](https://learn.microsoft.com/en-us/power-bi/visuals/power-bi-visualization-anomaly-detection))
- **ThoughtSpot SpotIQ change analysis** compares **two user-selected data
  points** (i.e., two periods at whatever grain the chart is at) and slices
  the delta by attributes — pure two-period decomposition, sidestepping grain
  by construction.
  ([SpotIQ change analysis](https://docs.thoughtspot.com/cloud/26.7.0.cl/spotiq-change))
- **Sundial** (sundial.ai, ex-Meta founders; OpenAI case study; $23M raise)
  does automated diagnostics/anomaly RCA on business metrics but publishes
  nothing about grain internals. **Falkon** similarly undocumented (and
  appears defunct). **Kausa** (dimensional driver testing) likewise.
  **DoubleLoop** metric trees are strategy-visualization (acquired by
  Mixpanel, which launched "Metric Trees" in-platform Aug 2025); no evidence
  of statistical grain handling.
  ([Sundial](https://www.sundial.ai/),
  [funding](https://www.hpcwire.com/bigdatawire/this-just-in/sundial-raises-23m-to-democratize-data-insights-beyond-dashboards/),
  [DoubleLoop KPI trees](https://doubleloop.app/solutions/kpi_trees),
  [Mixpanel metric trees](https://mixpanel.com/blog/metric-trees-benefits-guide/),
  [Levers Labs RCA guide](https://www.leverslabs.com/article/root-cause-analysis-with-metric-trees) —
  mentions "temporal variance" as a drift factor but no grain mechanics)

**Takeaway:** no commercial tool models *below* a metric's declared grain;
per-metric minimum grain (Tableau Pulse) is the closest documented design and
matches the proposal.

## 5. Snapshot vs flow metrics

- The semantic-layer world distinguishes **additive flow measures** (sum over
  any window) from **non-additive/snapshot measures** (balances, MRR, account
  totals) that must be aggregated over time by **last-value / first-value /
  max-min-at-boundary**, not sum. MetricFlow's mechanism is
  **`non_additive_dimension`** on a measure: name the time dimension the
  measure is non-additive over, choose `window_choice: min|max` (start- or
  end-of-window value), optionally `window_groupings`. There is deliberately
  no `sum`-over-time for these.
  ([Measures](https://docs.getdbt.com/docs/build/measures),
  [semantic layer spec discussion](https://github.com/dbt-labs/dbt-core/discussions/7456))
- When querying a snapshot metric at coarser grain, MetricFlow picks the
  boundary value within the window (start by default) — i.e., resampling a
  snapshot up is **`.last()`/`.first()`, never `.sum()`**; resampling a flow
  up is `.sum()`; rates/ratios re-derive from re-aggregated numerator and
  denominator.
- **Looker symmetric aggregates** solve the spatial analog (join fan-out
  double-counting a measure stored at a different grain than the query) via
  `SUM(DISTINCT hash + value)`. The lesson transfers: the semantic layer
  *stores the measure's native grain and guards every aggregation across grain
  boundaries automatically* rather than trusting the query author.
  ([symmetric_aggregates](https://cloud.google.com/looker/docs/reference/param-explore-symmetric-aggregates),
  [explainer](https://cloud.google.com/looker/docs/best-practices/understanding-symmetric-aggregates))

**Takeaway:** breakdown needs a per-node **temporal aggregation operator**
alongside grain — `sum` (flows), `last` (snapshots), recompute-from-components
(rates) — because "resample-up" is only well-defined once you know which one
applies. This is also what makes the MRR step-function artifact disappear: MRR
resampled to month via `last` is one honest observation per month.

## Implications for breakdown

**(a) Per-node grain declaration — yes, per-node, not global.** Every
precedent (MetricFlow's column-level granularity + metric-level
`time_granularity`, Tableau Pulse's per-metric minimum grain) is per-metric
with a coarsen-only rule. Recommend: optional `grain: day|week|month` on each
node (default `day` for back-compat), plus kind metadata (flows sum, snapshots
last). Enforce MetricFlow's invariant — a node may be *queried/coarsened*
upward but never analyzed below its declared grain. The constraint "all
parents of a formula node share the node's grain" is stricter than MetricFlow
(which auto-coarsens inputs to the common grain); a friendlier v1:
**auto-coarsen finer parents up to the formula node's grain using each
parent's aggregation kind, and hard-error only when a parent is coarser than
the child**. That keeps `signups(day) → MRR(month)` trees authorable without
boilerplate while forbidding the impossible direction.

**(b) Aggregate-then-fit — yes, for both edge types, with different
rationales.** For *deterministic* edges the identity must hold in the data
actually compared; since E[BC] ≠ E[B]E[C] in general, the only self-consistent
choice is to decompose at the node's declared grain (attribution of a
month-grain metric runs on month-grain parent values). For *probabilistic*
edges, fitting BSTS on daily step-functions of a monthly snapshot manufactures
~30× fake sample size and spurious covariance — the temporal-aggregation and
granularity-paradox literature says model at the natural grain. Fit each
probabilistic edge at the coarser of (parent grain, child grain). Fewer
observations per fit is the honest posterior width, not a regression. The
principled future upgrade (roadmap, not MVP, per working agreements) is a
mixed-frequency state-space model: latent daily/weekly state with coarse
observations — PyMC supports this via missing data, and it's exactly the
Bai–Ghysels–Wright construction.

**(c) Keeping covariance when aggregating — surface it, don't split it
silently.** Adopt the two-level view: the window/period headline is the
**Bennet/Sun/Shapley decomposition of the aggregates** — for Y=B·C over
window w: ΔY ≈ ΔB̄·C̄ + ΔC̄·B̄ + interaction, where the "interaction" bucket
explicitly contains both the Shapley ΔB·ΔC term *and* the within-window
covariance change Δcov(B,C) from E[BC] = E[B]E[C] + cov(B,C). Label it (e.g.,
"mix/covariance") rather than folding it 50/50 into the factors — the
de Bruyn/Casler critique of Sun's silent split is precisely that it distorts
when the interaction is large, and in business terms a large covariance term
("your price and volume started co-moving") *is a finding*, not a nuisance.
The drill-down view then decomposes cov(B,C) per fine-grain period for nodes
whose parents are stored finer than the analysis window. This makes the
fine-vs-coarse discrepancy an explicit, named row in the attribution table
instead of a reconciliation bug.

One caution from the tool survey: nothing else on the market does per-node
grain with explicit covariance attribution — that's an opportunity for
breakdown, but it also means no UX prior exists; the "mix/covariance" row
will need careful explanation in `docs/model.md`.
