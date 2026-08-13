"""The README is the PyPI landing page — it ships as the wheel's METADATA
long_description — so a defect in it is a defect in the published artifact.

Nothing executed the README before this module, and it drifted in several places
at once: YAML examples that no longer parsed cleanly, a route documented nowhere,
a deep link the MCP server stopped minting, and a transcript whose every figure
had gone stale under the mock generator. Every one of them was the same failure —
the examples were prose, and prose does not run.

Three parts, deliberately disciplined about what they skip:

1. **Every ```yaml block is fed to the real parser.** Excerpts whose parents
   live elsewhere in the README are *stubbed*, not "fixed" — the stubbing is
   named in the failure message so a future reader knows what was synthesized.
   Blocks that are not metric definitions at all are skipped, but the skipped
   set is asserted exactly, so a block that quietly stops parsing cannot hide
   as "just a fragment". Parser **warnings** fail too: a passing parse that
   lints the reader's own tree is still a bad example.
2. **Every documented curl is issued against the real app.** Route template,
   query-parameter names and status are all checked; the not-replayed set is
   asserted exactly, for the same reason.
3. **The MCP section's figures are re-run.** The transcript quoted numbers no
   code had produced in months; this pins them.
"""

import json
import logging
import re
import shlex
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

import pytest
import yaml

pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from breakdown.api.main import app  # noqa: E402
from breakdown.parser import Parser  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
README_PATH = REPO / "README.md"
README = README_PATH.read_text()
MAIN_PY = (REPO / "breakdown" / "api" / "main.py").read_text()
DEMO_TREE = REPO / "breakdown" / "examples" / "jaffle_shop_tree.yml"


def _unquote_blockquotes(text: str) -> str:
    """Strip markdown blockquote markers line-for-line (line numbers survive).

    Without this, a fenced block inside a `>` callout is invisible to the
    extractor — which would make "put the example in a blockquote" a way to
    escape every check in this module.
    """
    return "\n".join(re.sub(r"^\s{0,3}> ?", "", line) for line in text.splitlines())


README_UNQUOTED = _unquote_blockquotes(README)


def _path_matches(template: str, path: str) -> bool:
    """Does a concrete path fill a route template (`/rca/{name}`)?"""
    parts = re.split(r"\{[^}]*\}", template)
    return re.fullmatch("[^/]+".join(re.escape(p) for p in parts), path) is not None


# --------------------------------------------------------------------------
# (a) Every ```yaml block parses, cleanly
# --------------------------------------------------------------------------

_YAML_FENCE = re.compile(r"^```yaml[ \t]*\n(.*?)^```[ \t]*$", re.S | re.M)

# Blocks that are not metric definitions and cannot be parsed as a tree, keyed
# by their top-level YAML key. Asserted as a *sorted list*, so both identity and
# count matter: an example that silently stopped looking like a metric
# definition lands here and fails, rather than disappearing into a skip.
EXPECTED_SKIPPED_YAML = sorted(
    [
        "bind",  # the count_distinct entity-grain binding excerpt
        "priors",  # the shared-coefficient prior example
        "priors",  # the per-parent prior override example
        "seasonality",  # the seasonality component example
        "tree",  # the `tree:` identity/goal block
    ]
)

# `kind` is declared explicitly on synthesized parents so the parser's
# ratio-shaped-name lint fires on the README's own metrics and never on our
# scaffolding — stubs like `cohort_rate` and `average_order_value` would
# otherwise trip it and blame the README for something we wrote.
_STUB_KIND = "flow"


class YamlBlock:
    def __init__(self, index: int, line: int, body: str):
        self.index = index
        self.line = line
        self.body = body

    @property
    def where(self) -> str:
        return f"README.md ```yaml block #{self.index} (line {self.line})"


def _yaml_blocks() -> list:
    blocks = []
    for i, m in enumerate(_YAML_FENCE.finditer(README_UNQUOTED), start=1):
        line = README_UNQUOTED[: m.start()].count("\n") + 1
        blocks.append(YamlBlock(i, line, m.group(1)))
    return blocks


YAML_BLOCKS = _yaml_blocks()


def _stub_for(name: str) -> dict:
    return {"name": name, "source": f"readme_test.stubbed.{name}", "kind": _STUB_KIND}


def _classify(data):
    """-> 'tree' | 'provider-only' | 'metric-list' | 'fragment'."""
    if isinstance(data, dict) and "metrics" in data:
        return "tree"
    if isinstance(data, dict) and "provider" in data:
        return "provider-only"
    if isinstance(data, list) and data and all(isinstance(d, dict) and "name" in d for d in data):
        return "metric-list"
    return "fragment"


def _referenced_elsewhere(metrics: list) -> list:
    """Names an excerpt leans on that it does not itself define — parents, and
    the `weight` metric a rate dimension blends by."""
    defined = {m["name"] for m in metrics}
    referenced = set()
    for m in metrics:
        referenced.update(m.get("parents") or [])
        for spec in (m.get("dimensions") or {}).values():
            if isinstance(spec, dict) and spec.get("weight"):
                referenced.add(spec["weight"])
    return sorted(referenced - defined)


def _as_parsable_document(data):
    """-> (document, synthesized_names) or (None, None) for a fragment.

    Excerpts legitimately reference metrics defined elsewhere in the README, so
    the missing ones are synthesized rather than invented *into* the README.
    Every other parser rule — required `source`, valid `kind`, formula
    validation, prior keys, grain nesting, dimension weights — still applies.
    """
    kind = _classify(data)
    if kind == "fragment":
        return None, None
    if kind == "provider-only":
        # The provider reference blocks carry no metrics; one stub metric lets
        # the provider block itself be validated (type enum, ${VAR} expansion).
        return (
            {"provider": data["provider"], "metrics": [_stub_for("readme_probe")]},
            ["readme_probe (this block declares no metrics of its own)"],
        )
    if kind == "metric-list":
        metrics, provider = list(data), None
    else:
        metrics, provider = list(data["metrics"]), data.get("provider")
    missing = _referenced_elsewhere(metrics)
    doc = {"metrics": [_stub_for(n) for n in missing] + metrics}
    if provider is not None:
        doc["provider"] = provider
    return doc, missing


def _stub_note(synthesized: list) -> str:
    if not synthesized:
        return ""
    return (
        f"\n\nNOTE: this block is an excerpt, so the test synthesized "
        f"{len(synthesized)} metric(s) it references but does not define: "
        f"{', '.join(synthesized)} "
        f"(each as {{source: readme_test.stubbed.<name>, kind: {_STUB_KIND}}}). "
        f"They are scaffolding, not README content — if the failure is about one "
        f"of them, this test is wrong rather than the README."
    )


# Parametrized over the parsable blocks only, rather than skipping the
# fragments at runtime: the sdist CI job audits every skip reason in the shipped
# suite against a fixed allow-list, and "this block is a fragment" is not one of
# the three shapes it accepts. The fragments are covered instead by the
# skip-list test and the block-count tripwire below, which is where the
# accounting belongs anyway.
PARSABLE_BLOCKS = [b for b in YAML_BLOCKS if _classify(yaml.safe_load(b.body)) != "fragment"]


@pytest.mark.parametrize("block", PARSABLE_BLOCKS, ids=lambda b: f"L{b.line}")
def test_readme_yaml_block_parses_without_warnings(block, caplog):
    """Fail on the parser's warnings as well as its exceptions: the flagship
    example used to parse successfully while emitting the `kind: rate` lint —
    a passing parse that told the reader their own tree was wrong."""
    doc, synthesized = _as_parsable_document(yaml.safe_load(block.body))
    note = _stub_note(synthesized)

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="breakdown"):
        try:
            Parser(yaml.safe_dump(doc, sort_keys=False))
        except Exception as exc:  # noqa: BLE001 — the README's own error is the message
            pytest.fail(f"{block.where} does not parse: {exc}{note}\n\n{block.body}")
    warnings = [
        r.getMessage()
        for r in caplog.records
        if r.levelno >= logging.WARNING and r.name.startswith("breakdown")
    ]
    assert not warnings, (
        f"{block.where} parses, but the parser warns about it — a documented "
        "example must not lint. Warnings:\n  - "
        + "\n  - ".join(warnings)
        + f"{note}\n\n{block.body}"
    )


def test_readme_yaml_skip_list_is_exactly_what_we_expect():
    """A block that silently stops being a metric definition must fail here
    rather than vanish into "it's just a fragment"."""
    skipped = []
    for block in YAML_BLOCKS:
        data = yaml.safe_load(block.body)
        if _classify(data) == "fragment":
            key = ",".join(data) if isinstance(data, dict) else type(data).__name__
            skipped.append((key, block.line))

    assert sorted(k for k, _ in skipped) == EXPECTED_SKIPPED_YAML, (
        "The set of README YAML blocks skipped as non-metric fragments changed.\n"
        f"  found (key, line): {sorted(skipped)}\n"
        f"  expected keys:     {EXPECTED_SKIPPED_YAML}\n"
        "If a real example moved into this list it stopped looking like a metric "
        "definition — fix the README, not this list."
    )


def test_readme_has_the_yaml_blocks_we_think_it_has():
    """A tripwire: if the extractor stops seeing blocks (a fence style change, a
    deleted section), every parametrized case above passes by not existing."""
    assert len(YAML_BLOCKS) == 19, f"expected 19 ```yaml blocks, found {len(YAML_BLOCKS)}"
    assert len(PARSABLE_BLOCKS) == 14, (
        f"expected 14 parsable ```yaml blocks, found {len(PARSABLE_BLOCKS)} — "
        "a documented example stopped being one, or a new one appeared unchecked"
    )


# --------------------------------------------------------------------------
# (b) Every documented HTTP example actually works
# --------------------------------------------------------------------------


class CurlExample:
    def __init__(self, line: int, method: str, path: str, query: str, body, headers: dict):
        self.line = line
        self.method = method
        self.path = path
        self.query = query
        self.body = body
        self.headers = headers

    @property
    def name(self) -> str:
        return f"{self.method} {self.path}" + (f"?{self.query}" if self.query else "")

    @property
    def where(self) -> str:
        return f"{self.name}  (README.md line {self.line})"


def _parse_curl(command: str, line: int) -> CurlExample:
    tokens = shlex.split(command, comments=True)
    method = url = body = None
    headers = {}
    idx = 1
    while idx < len(tokens):
        tok = tokens[idx]
        if tok in ("-X", "--request"):
            idx += 1
            method = tokens[idx]
        elif tok in ("-H", "--header"):
            idx += 1
            key, _, value = tokens[idx].partition(":")
            headers[key.strip()] = value.strip()
        elif tok in ("-d", "--data", "--data-raw"):
            idx += 1
            body = tokens[idx]
        elif tok.startswith("-") or tok == "&":
            pass  # -s/-f have no bearing; `curl … &` is backgrounding, not request
        elif url is None:
            url = tok
        idx += 1
    assert url is not None, f"no URL in the curl on README line {line}: {command}"
    parsed = urlparse(url if "://" in url else "http://" + url)
    return CurlExample(
        line=line,
        method=(method or "GET").upper(),
        path=parsed.path,
        query=parsed.query,
        body=body,
        headers=headers,
    )


def _curl_examples() -> list:
    """Extract each `curl` invocation, joining shell continuations *and* any
    lines needed to close an open quote (the `/simulate` body spans five)."""
    lines = README_UNQUOTED.splitlines()
    examples = []
    i = 0
    while i < len(lines):
        if not re.match(r"^\s*curl(\s|$)", lines[i]):
            i += 1
            continue
        start, raw, j = i + 1, lines[i], i
        while j + 1 < len(lines) and _needs_more(raw):
            j += 1
            raw = raw.rstrip().removesuffix("\\") + "\n" + lines[j]
        examples.append(_parse_curl(raw, start))
        i = j + 1
    return examples


def _needs_more(raw: str) -> bool:
    if raw.rstrip().endswith("\\"):
        return True
    try:
        shlex.split(raw, comments=True)
    except ValueError:
        return True
    return False


CURL_EXAMPLES = _curl_examples()

# Examples NOT issued against the app, each with its reason. Asserted exactly:
# a route quietly falling out of the replayable set is a failure, not a silent
# skip. Everything here still has its route template and its query-parameter
# names checked below — only the request itself is not made.
NOT_REPLAYED = {
    "POST /analyze/order_count": (
        "the bare form runs the NUTS default (4 chains x 500 draws after 500 tune) "
        "and takes minutes; the documented ADVI form on the same route is replayed"
    ),
    "POST /analyze/order_count?inference_method=nuts&draws=1000": (
        "NUTS with 1000 draws x 4 chains — same cost, same route as the replayed ADVI example"
    ),
    "POST /trees/marketing/rca/paid_signups?analysis_start=2026-08-01&analysis_end=2026-08-07": (
        "names the `marketing` tree and its `paid_signups` metric from the "
        "several-trees illustration; no such tree ships with the package"
    ),
}

# Examples replayed on a synthesized single-metric tree rather than the bundled
# one, because they name `signups` and a declared `region` dimension and the
# bundled jaffle tree declares no dimensions. Route, parameter names and status
# are all still genuinely exercised.
SLICE_FIXTURE_TREE = """
provider:
  type: mock
metrics:
  - name: signups
    source: my.metrics.signups
    dimensions:
      region: customer__region
"""
ON_SLICE_FIXTURE = {
    "GET /metrics/signups/query?dimension=region",
    "POST /rca/signups/slices?dimension=region&reference_start=2024-02-05"
    "&reference_end=2024-03-03&analysis_start=2024-03-04&analysis_end=2024-03-10",
}

# Replayed examples are expected to succeed; stated as a table so a deliberate
# 4xx example could be documented as one.
EXPECTED_STATUS: dict = {}

# The windows the MCP section's exchange quotes.
TRANSCRIPT_WINDOWS = {
    "reference_start": "2024-03-13",
    "reference_end": "2024-03-26",
    "analysis_start": "2024-03-27",
    "analysis_end": "2024-04-09",
}

# The commentary under the transcript claims that the *same* tree over a
# four-week pair returns an order-count interval excluding zero, and a
# different story — and uses that to say why moving the transcript to the
# longer window would have been window-shopping. That is a claim about a run,
# so it is a run.
FOUR_WEEK_WINDOWS = {
    "reference_start": "2024-02-14",
    "reference_end": "2024-03-12",
    "analysis_start": "2024-03-13",
    "analysis_end": "2024-04-09",
}


@pytest.fixture(scope="module")
def replayed(tmp_path_factory):
    """One pass over the app: boot the bundled tree, issue every replayable
    curl against it, run the MCP section's two RCAs, then boot the slice
    fixture.

    Module-scoped and single-pass because `app` is a process-wide singleton (two
    TestClient lifespans must not overlap) and because on-demand ADVI fits are
    cached per fit window — one session costs three fits rather than one per
    test.
    """
    results = {}

    def issue(client, ex: CurlExample):
        kwargs = {"params": dict(parse_qsl(ex.query)), "headers": ex.headers}
        if ex.body is not None:
            kwargs["json"] = json.loads(ex.body)
        return client.request(ex.method, ex.path, **kwargs)

    demo = [
        ex
        for ex in CURL_EXAMPLES
        if ex.name not in NOT_REPLAYED and ex.name not in ON_SLICE_FIXTURE
    ]
    fixture = [ex for ex in CURL_EXAMPLES if ex.name in ON_SLICE_FIXTURE]

    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("BREAKDOWN_TREE", str(DEMO_TREE))
        mp.setenv("BREAKDOWN_START_DATE", "2024-01-01")
        mp.setenv("BREAKDOWN_END_DATE", "2024-04-09")
        mp.delenv("BREAKDOWN_API_TOKEN", raising=False)
        mp.delenv("BREAKDOWN_REQUIRE_AUTH", raising=False)
        with TestClient(app) as client:
            for ex in demo:
                results[ex.name] = issue(client, ex)
            resp = client.post("/rca/revenue", params=TRANSCRIPT_WINDOWS)
            assert resp.status_code == 200, resp.text
            transcript = resp.json()
            resp = client.post("/rca/revenue", params=FOUR_WEEK_WINDOWS)
            assert resp.status_code == 200, resp.text
            four_week = resp.json()

    tree_file = tmp_path_factory.mktemp("readme") / "slice_fixture.yml"
    tree_file.write_text(SLICE_FIXTURE_TREE)
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("BREAKDOWN_TREE", str(tree_file))
        mp.setenv("BREAKDOWN_START_DATE", "2024-01-01")
        mp.setenv("BREAKDOWN_END_DATE", "2024-04-09")
        with TestClient(app) as client:
            for ex in fixture:
                results[ex.name] = issue(client, ex)

    return results, transcript, four_week


def test_readme_has_the_curl_examples_we_think_it_has():
    assert len(CURL_EXAMPLES) == 15, (
        f"expected 15 curl examples in README.md, found {len(CURL_EXAMPLES)}: "
        f"{[e.where for e in CURL_EXAMPLES]}"
    )


def test_every_documented_curl_hits_a_real_route():
    """Catches a route that no longer exists — including for the examples that
    are too expensive to replay.

    Read off the OpenAPI schema rather than by walking `app.routes`, which is
    not a flat list of routes and only looked like one. FastAPI **0.137.0**
    changed `include_router` to append a single lazy `_IncludedRouter` node per
    include instead of copying each route into the parent, so from 0.137.0 on
    this walk saw two objects with no `.path` where it used to see twenty, and
    reported a fully-working app as twenty missing routes. Nothing about the
    app changed: every path still serves, and the schema's 25 paths and 25
    unique operation ids are the same set either side of the change (measured
    on 0.135.2 through 0.141.1). `app.openapi()` is
    the public, resolved view and is what `_operation_for` below already used —
    this test was the neighbour that never got the same treatment."""
    templates = {
        (template, method.upper())
        for template, ops in app.openapi()["paths"].items()
        for method in ops
    }
    for ex in CURL_EXAMPLES:
        assert any(
            method == ex.method and _path_matches(path, ex.path) for path, method in templates
        ), (
            f"{ex.where} names a route the app does not serve. Registered "
            f"{ex.method} paths: {sorted(p for p, m in templates if m == ex.method)}"
        )


def _operation_for(spec: dict, ex: CurlExample):
    for template, ops in spec["paths"].items():
        if _path_matches(template, ex.path) and ex.method.lower() in ops:
            return ops[ex.method.lower()]
    return None


def test_every_documented_query_parameter_is_accepted():
    """Catches a renamed required parameter and a documented parameter the
    endpoint rejects, for every example including the unreplayed ones."""
    spec = app.openapi()
    for ex in CURL_EXAMPLES:
        op = _operation_for(spec, ex)
        assert op is not None, f"{ex.where}: no OpenAPI operation for this path"
        params = op.get("parameters", [])
        declared = {p["name"] for p in params if p["in"] == "query"}
        required = {p["name"] for p in params if p["in"] == "query" and p.get("required")}
        documented = {k for k, _ in parse_qsl(ex.query)}
        assert not documented - declared, (
            f"{ex.where} passes query parameter(s) {sorted(documented - declared)} that "
            f"{ex.method} {ex.path} does not accept. Declared: {sorted(declared)}"
        )
        assert not required - documented, (
            f"{ex.where} omits required query parameter(s) {sorted(required - documented)} — "
            "either the README is stale or the parameter was renamed."
        )


def test_replayed_curl_examples_return_the_documented_status(replayed):
    results, _, _ = replayed
    failures = []
    for ex in CURL_EXAMPLES:
        if ex.name in NOT_REPLAYED:
            continue
        resp = results[ex.name]
        expected = EXPECTED_STATUS.get(ex.name, 200)
        if resp.status_code != expected:
            failures.append(
                f"{ex.where}: expected {expected}, got {resp.status_code} — {resp.text[:400]}"
            )
    assert not failures, "Documented curl examples that no longer work:\n" + "\n".join(failures)


def test_curl_skip_list_is_exactly_what_we_expect():
    names = {ex.name for ex in CURL_EXAMPLES}
    assert not set(NOT_REPLAYED) - names, (
        "NOT_REPLAYED names curl examples the README no longer contains: "
        f"{sorted(set(NOT_REPLAYED) - names)}. Drop them, or the skip list protects nothing."
    )
    assert not ON_SLICE_FIXTURE - names, (
        "ON_SLICE_FIXTURE names curl examples the README no longer contains: "
        f"{sorted(ON_SLICE_FIXTURE - names)}"
    )


# --------------------------------------------------------------------------
# The API reference table documents every route
# --------------------------------------------------------------------------

_ROUTE_DECORATOR = re.compile(
    r"^@(?:app|router)\.(get|post|put|patch|delete)\(\s*[\"']([^\"']+)[\"']", re.M
)


def _normalize(path: str) -> str:
    """`/trees/{tree_id}/load` and the README's `/trees/{id}/load` are the same
    route; the placeholder's spelling is not part of the contract."""
    return re.sub(r"\{[^}]*\}", "{}", path)


def _api_reference_paths() -> set:
    """Paths named in a table *cell* of the API reference section.

    A whole cell, not a substring: scanning for backticked runs anywhere in the
    line makes prose like ``grains`/`kinds`` yield a phantom ``/`` entry, and
    this assertion then passes by accident — which is how it read on its first
    run.
    """
    start = README.index("\n## API reference")
    end = README.index("\n## ", start + 1)
    found = set()
    for line in README[start:end].splitlines():
        if not line.lstrip().startswith("|"):
            continue
        for cell in line.strip().strip("|").split("|"):
            m = re.fullmatch(r"`(/[^`]*)`", cell.strip())
            if m:
                found.add(_normalize(m.group(1)))
    return found


def test_api_reference_table_documents_every_route():
    """The assertion that would have caught `GET /metrics/{name}/query` being
    documented nowhere."""
    declared = {_normalize(p) for _, p in _ROUTE_DECORATOR.findall(MAIN_PY)}
    missing = sorted(declared - _api_reference_paths())
    assert not missing, (
        "Routes defined in breakdown/api/main.py but absent from the README's API "
        f"reference tables: {missing}. A route nobody documents is a route nobody uses."
    )


# --------------------------------------------------------------------------
# (c) The MCP section's figures come from a run we actually performed
# --------------------------------------------------------------------------

# Tolerances, and why:
#
#   tight (<=1e-3)  Everything read off the data — window means, gaps, relative
#                   changes — and every Shapley contribution on the `revenue`
#                   formula node. The mock generator is seeded and the Shapley
#                   decomposition is exact arithmetic over the series, so these
#                   reproduce to floating point; the tolerance only absorbs the
#                   rounding in the README's own prose.
#
#   bands           The `daily_sessions -> order_count` contribution comes from
#                   a seeded ADVI fit, and every interval from a seeded
#                   bootstrap. Seeded is not version-stable — a PyMC/pytensor
#                   or numpy bump moves the last digits — so these are bands
#                   wide enough to survive a minor upgrade and narrow enough
#                   that the README's qualitative claims ("about two-thirds",
#                   "both cross zero", "direction not established") fail with
#                   them.


def test_mcp_section_figures_still_match_a_real_run(replayed):
    _, rca, _ = replayed
    nodes = rca["nodes"]

    def q(name, field):
        return nodes[name][field]

    # --- headline: revenue rose; it did not fall -------------------------
    assert q("revenue", "baseline") == pytest.approx(26386.5, rel=1e-4)
    assert q("revenue", "actual") == pytest.approx(26982.1, rel=1e-4)
    assert q("revenue", "gap") == pytest.approx(595.5, rel=1e-3)
    assert q("revenue", "relative_change") == pytest.approx(0.0226, rel=5e-3)

    # --- the two movers --------------------------------------------------
    assert q("order_count", "baseline") == pytest.approx(142.7, rel=1e-3)
    assert q("order_count", "actual") == pytest.approx(148.0, rel=1e-3)
    assert q("order_count", "relative_change") == pytest.approx(0.0376, rel=5e-3)

    assert q("average_order_value", "baseline") == pytest.approx(184.68, rel=1e-4)
    assert q("average_order_value", "actual") == pytest.approx(182.15, rel=1e-4)
    assert q("average_order_value", "relative_change") == pytest.approx(-0.0137, rel=5e-3)

    assert q("daily_sessions", "relative_change") == pytest.approx(0.0224, rel=5e-3)

    # --- the Shapley split the narration is built on ---------------------
    contrib = {c["parent"]: c for c in q("revenue", "contributions")}
    oc, aov = contrib["order_count"], contrib["average_order_value"]

    assert oc["estimate"] == pytest.approx(984.5, rel=1e-3)
    assert oc["share_of_gap"] == pytest.approx(1.653, rel=1e-3)
    assert aov["estimate"] == pytest.approx(-367.1, rel=1e-3)
    assert aov["share_of_gap"] == pytest.approx(-0.616, rel=1e-3)

    # The transcript quotes the pair's sum against the observed gap to argue
    # that nothing is hiding in the remainder.
    assert oc["estimate"] + aov["estimate"] == pytest.approx(617.4, rel=1e-3)

    # --- the spine of the narration: exact split, undetermined direction --
    # Both legs' intervals cross zero over a 14-period window, which is why the
    # transcript declines to call either one established. If a change ever
    # makes one of these exclude zero, the section's argument is void and the
    # prose has to be rewritten — not the number quietly bumped.
    assert oc["ci_95"][0] < 0 < oc["ci_95"][1], (
        "The README says the order-count leg's interval crosses zero; it no longer does."
    )
    assert aov["ci_95"][0] < 0 < aov["ci_95"][1], (
        "The README says the AOV leg's interval crosses zero too — the whole point of the "
        "section is that neither direction is established at this window length."
    )
    assert oc["prob_same_direction"] == pytest.approx(0.78, abs=0.04)
    assert aov["prob_same_direction"] == pytest.approx(0.92, abs=0.04)
    assert oc["ci_95"] == pytest.approx([-1499, 3365], rel=0.05)
    assert aov["ci_95"] == pytest.approx([-909, 119], rel=0.05)

    # --- `unexplained` is small, which the narration cites ----------------
    unexplained = q("revenue", "unexplained")
    assert abs(unexplained) == pytest.approx(21.9, rel=0.02)
    assert abs(unexplained / q("revenue", "gap")) < 0.04, (
        "The README says `unexplained` is under 4% of the gap."
    )

    # --- the sessions leg: about two-thirds, and the least settled --------
    sessions = {c["parent"]: c for c in q("order_count", "contributions")}["daily_sessions"]
    assert sessions["estimate"] == pytest.approx(3.7, abs=0.6)
    assert 0.60 < sessions["share_of_gap"] < 0.76, (
        "The README says sessions explain 'about two-thirds' of the order-count gain."
    )
    assert sessions["ci_95"] == pytest.approx([-9.6, 17.4], rel=0.25)
    assert sessions["ci_95"][0] < 0 < sessions["ci_95"][1], (
        "The README says the sessions leg's interval crosses zero."
    )
    assert sessions["prob_same_direction"] == pytest.approx(0.69, abs=0.06)
    assert sessions["prob_same_direction"] < oc["prob_same_direction"], (
        "The README calls the sessions leg 'the least settled leg of the three'."
    )
    assert sessions["prob_same_direction"] < aov["prob_same_direction"]

    # The README says a node declaring no `seasonality` carries no `seasonal`
    # key rather than a 0.0 with a zero-width interval. `order_count` declares
    # none; `revenue` does, but is a formula node with no `components` at all.
    assert set(q("order_count", "components")) == {"trend"}

    # --- ranked_causes: the three scores the README quotes ---------------
    ranked = rca["ranked_causes"]
    assert [r["metric"] for r in ranked] == [
        "order_count",
        "daily_sessions",
        "average_order_value",
    ]
    assert [r["via"] for r in ranked] == ["revenue", "order_count", "revenue"]
    scores = {r["metric"]: r["score"] for r in ranked}
    # `order_count` and `average_order_value` hang off exact Shapley shares, so
    # they are arithmetic; `daily_sessions` inherits the fitted sessions share.
    assert scores["order_count"] == pytest.approx(0.44, abs=0.005)
    assert scores["average_order_value"] == pytest.approx(0.27, abs=0.005)
    assert scores["daily_sessions"] == pytest.approx(0.30, abs=0.03)
    # The claim the README makes *about* the score, rather than its value: a
    # parent explaining 165% of a gap 62% of which was cancelled ranks below a
    # lone parent cleanly explaining 80% — which under this weighting would
    # score 0.8. A clamp that saturated at 1.0 would fail this (roadmap C5).
    assert scores["order_count"] < 0.8, (
        "The README says order_count's 165% share scores *below* a clean 80% explainer."
    )


def test_readme_four_week_comparison_is_a_run_we_performed(replayed):
    """The commentary argues that moving the transcript to a longer window to
    get a cleaner interval would have been window-shopping — and it can only
    argue that because the longer window really does come back cleaner, and
    really does tell a different story. Both halves are claims about a run."""
    _, _, four_week = replayed
    revenue = four_week["nodes"]["revenue"]

    assert revenue["gap"] < 0, (
        "The README says revenue *fell* over the four-week pair — that is what makes "
        "switching to it a different story rather than a cleaner telling of this one."
    )
    oc = {c["parent"]: c for c in revenue["contributions"]}["order_count"]
    lo, hi = oc["ci_95"]
    assert lo < hi < 0, (
        "The README says the four-week order-count interval excludes zero. If it no "
        "longer does, the point about window-shopping needs restating, not deleting: "
        f"got [{lo:.1f}, {hi:.1f}]."
    )


def test_readme_report_url_is_the_link_the_mcp_server_actually_mints(monkeypatch):
    """The transcript closes on a deep link and claims it replays the analysis.

    It stopped doing so when trees gained ids: `run_rca` always passes
    `tree=state.id`, so every minted link now carries a `#tree=` prefix — which
    the README says itself, two paragraphs above, while printing a link without
    one.
    """
    from breakdown.mcp.shaping import rca_link

    monkeypatch.delenv("BREAKDOWN_PUBLIC_URL", raising=False)
    monkeypatch.delenv("BREAKDOWN_PORT", raising=False)
    expected = rca_link(
        "revenue",
        TRANSCRIPT_WINDOWS["reference_start"],
        TRANSCRIPT_WINDOWS["reference_end"],
        TRANSCRIPT_WINDOWS["analysis_start"],
        TRANSCRIPT_WINDOWS["analysis_end"],
        tree=DEMO_TREE.stem,
    )
    assert expected in README, (
        "The MCP section's `report_url` is not what the server would mint for "
        f"this analysis. Expected to find:\n  {expected}"
    )


def test_readme_does_not_call_the_mcp_example_an_unedited_transcript():
    """Its figures are regenerated by the test above and its narration is
    written prose, so the label cannot claim a captured model response.
    Relabelling was the whole point of touching that section."""
    section = README[README.index("### What it looks like") :]
    section = section[: section.index("\n### ")].lower()
    for forbidden in ("unedited", "verbatim"):
        assert forbidden not in section, (
            f"The MCP example section calls itself {forbidden!r}. Its figures are "
            "regenerated by a run this module performs and its narration is written "
            "prose — the label has to say so."
        )
