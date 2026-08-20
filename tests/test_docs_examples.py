"""The user-facing docs are executed, not proofread.

The README ships as the wheel's METADATA long_description — it *is* the PyPI
landing page — and the two reference pages beside it are where anyone authoring
a tree or calling the API actually lives. Nothing executed any of it before this
module, and the README drifted in several places at once: YAML examples that no
longer parsed cleanly, a route documented nowhere, a deep link the MCP server
stopped minting, and a transcript whose every figure had gone stale under the
mock generator. Every one of them was the same failure — the examples were
prose, and prose does not run.

The guard is parameterized over `DOCS` below, one entry per documentation file,
because the reference material has been split out of the README more than once
now (`docs/yaml-reference.md`, `docs/api-reference.md`, and most recently
`docs/deploying.md`). A move that carried the content out from under a
README-only test would silently delete the guard, so the file list — not any
single path — is what this module scans. Adding a page is one entry.

Three parts, deliberately disciplined about what they skip:

1. **Every ```yaml block is fed to the real parser.** Excerpts whose parents
   live elsewhere in the docs are *stubbed*, not "fixed" — the stubbing is
   named in the failure message so a future reader knows what was synthesized.
   Blocks that are not metric definitions at all are skipped, but the skipped
   set is asserted exactly **per file**, so a block that quietly stops parsing
   cannot hide as "just a fragment". Parser **warnings** fail too: a passing
   parse that lints the reader's own tree is still a bad example.
2. **Every documented curl is issued against the real app.** Route template,
   query-parameter names and status are all checked; the not-replayed set is
   asserted exactly, per file, for the same reason.
3. **The MCP page's worked session is re-run over the MCP wire protocol.**
   The transcript quoted numbers no code had produced in months; this pins
   them. The session lives in docs/mcp.md and runs against the White Cube
   demo snapshots, so its tests skip where demo/ is absent (it is repo-only).
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
MAIN_PY = (REPO / "breakdown" / "api" / "main.py").read_text()
DEMO_TREE = REPO / "breakdown" / "examples" / "jaffle_shop_tree.yml"


def _unquote_blockquotes(text: str) -> str:
    """Strip markdown blockquote markers line-for-line (line numbers survive).

    Without this, a fenced block inside a `>` callout is invisible to the
    extractor — which would make "put the example in a blockquote" a way to
    escape every check in this module.
    """
    return "\n".join(re.sub(r"^\s{0,3}> ?", "", line) for line in text.splitlines())


def _path_matches(template: str, path: str) -> bool:
    """Does a concrete path fill a route template (`/rca/{name}`)?"""
    parts = re.split(r"\{[^}]*\}", template)
    return re.fullmatch("[^/]+".join(re.escape(p) for p in parts), path) is not None


_YAML_FENCE = re.compile(r"^```yaml[ \t]*\n(.*?)^```[ \t]*$", re.S | re.M)

# `kind` is declared explicitly on synthesized parents so the parser's
# ratio-shaped-name lint fires on the docs' own metrics and never on our
# scaffolding — stubs like `cohort_rate` and `average_order_value` would
# otherwise trip it and blame the docs for something we wrote.
_STUB_KIND = "flow"


class YamlBlock:
    def __init__(self, doc: "DocFile", index: int, line: int, body: str):
        self.doc = doc
        self.index = index
        self.line = line
        self.body = body

    @property
    def id(self) -> str:
        return f"{self.doc.name}:L{self.line}"

    @property
    def where(self) -> str:
        return f"{self.doc.name} ```yaml block #{self.index} (line {self.line})"


class CurlExample:
    def __init__(
        self, doc: "DocFile", line: int, method: str, path: str, query: str, body, headers: dict
    ):
        self.doc = doc
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
    def key(self) -> tuple:
        """Unique across files: the same route may be documented in two of them."""
        return (self.doc.name, self.name)

    @property
    def where(self) -> str:
        return f"{self.name}  ({self.doc.name} line {self.line})"


def _parse_curl(doc: "DocFile", command: str, line: int) -> CurlExample:
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
    assert url is not None, f"no URL in the curl on {doc.name} line {line}: {command}"
    parsed = urlparse(url if "://" in url else "http://" + url)
    return CurlExample(
        doc=doc,
        line=line,
        method=(method or "GET").upper(),
        path=parsed.path,
        query=parsed.query,
        body=body,
        headers=headers,
    )


def _needs_more(raw: str) -> bool:
    if raw.rstrip().endswith("\\"):
        return True
    try:
        shlex.split(raw, comments=True)
    except ValueError:
        return True
    return False


def _classify(data):
    """-> 'tree' | 'provider-only' | 'metric-list' | 'fragment'."""
    if isinstance(data, dict) and "metrics" in data:
        return "tree"
    if isinstance(data, dict) and "provider" in data:
        return "provider-only"
    if isinstance(data, list) and data and all(isinstance(d, dict) and "name" in d for d in data):
        return "metric-list"
    return "fragment"


class DocFile:
    """One documentation file, with the exact accounting we expect of it.

    Every count and every skip list is stated per file rather than in aggregate,
    so moving a section from one page to another has to move its expectations
    too — which is the whole point of the list: content that migrates cannot
    slip out of the guard on the way.
    """

    def __init__(
        self,
        path: str,
        *,
        yaml_blocks: int,
        parsable_yaml_blocks: int,
        skipped_yaml: list,
        curl_examples: int,
        not_replayed: dict | None = None,
        on_slice_fixture: set | None = None,
        has_route_table: bool = False,
    ):
        self.path = REPO / path
        self.name = self.path.name
        self.text = self.path.read_text()
        self.unquoted = _unquote_blockquotes(self.text)
        self.expected_yaml_blocks = yaml_blocks
        self.expected_parsable_yaml_blocks = parsable_yaml_blocks
        # Asserted as a *sorted list*, so both identity and count matter: an
        # example that silently stopped looking like a metric definition lands
        # here and fails, rather than disappearing into a skip.
        self.expected_skipped_yaml = sorted(skipped_yaml)
        self.expected_curl_examples = curl_examples
        # Examples NOT issued against the app, each with its reason. Asserted
        # exactly: a route quietly falling out of the replayable set is a
        # failure, not a silent skip. Everything here still has its route
        # template and its query-parameter names checked — only the request
        # itself is not made.
        self.not_replayed = not_replayed or {}
        self.on_slice_fixture = on_slice_fixture or set()
        self.has_route_table = has_route_table

        self.yaml_blocks = [
            YamlBlock(self, i, self.unquoted[: m.start()].count("\n") + 1, m.group(1))
            for i, m in enumerate(_YAML_FENCE.finditer(self.unquoted), start=1)
        ]
        self.curls = self._curl_examples()

    def _curl_examples(self) -> list:
        """Extract each `curl` invocation, joining shell continuations *and* any
        lines needed to close an open quote (the `/simulate` body spans five)."""
        lines = self.unquoted.splitlines()
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
            examples.append(_parse_curl(self, raw, start))
            i = j + 1
        return examples

    @property
    def parsable_blocks(self) -> list:
        return [b for b in self.yaml_blocks if _classify(yaml.safe_load(b.body)) != "fragment"]

    def __repr__(self) -> str:
        return self.name


# --------------------------------------------------------------------------
# The documentation files this module executes. One entry per file.
# --------------------------------------------------------------------------

DOCS = [
    DocFile(
        "README.md",
        yaml_blocks=2,
        parsable_yaml_blocks=2,
        skipped_yaml=[],
        curl_examples=2,
        not_replayed={
            "POST /analyze/order_count": (
                "the bare form runs the NUTS default (4 chains x 500 draws after 500 tune) "
                "and takes minutes; the documented ADVI form on the same route is replayed "
                "from docs/api-reference.md"
            ),
        },
    ),
    DocFile(
        "docs/deploying.md",
        yaml_blocks=0,
        parsable_yaml_blocks=0,
        skipped_yaml=[],
        curl_examples=3,
        not_replayed={
            "POST /trees/marketing/rca/paid_signups"
            "?analysis_start=2026-08-01&analysis_end=2026-08-07": (
                "names the `marketing` tree and its `paid_signups` metric from the "
                "several-trees illustration; no such tree ships with the package"
            ),
        },
    ),
    DocFile(
        "docs/first-tree-tutorial.md",
        # The tutorial builds the bundled jaffle tree from scratch, so its
        # blocks are the bundled tree in pieces (plus the three provider-swap
        # blocks and the dimensions teaser) and its curls replay against the
        # exact tree and window this fixture boots. That is the point: a
        # tutorial whose every command the suite has already run.
        yaml_blocks=7,
        parsable_yaml_blocks=7,
        skipped_yaml=[],
        curl_examples=2,
    ),
    DocFile(
        "docs/yaml-reference.md",
        yaml_blocks=20,
        parsable_yaml_blocks=15,
        skipped_yaml=[
            "bind",  # the count_distinct entity-grain binding excerpt
            "priors",  # the shared-coefficient prior example
            "priors",  # the per-parent prior override example
            "seasonality",  # the seasonality component example
            "tree",  # the `tree:` identity/goal block
        ],
        curl_examples=0,
    ),
    DocFile(
        "docs/api-reference.md",
        yaml_blocks=0,
        parsable_yaml_blocks=0,
        skipped_yaml=[],
        curl_examples=10,
        not_replayed={
            "POST /analyze/order_count?inference_method=nuts&draws=1000": (
                "NUTS with 1000 draws x 4 chains — same cost, same route as the "
                "replayed ADVI example"
            ),
        },
        # Replayed on a synthesized single-metric tree rather than the bundled
        # one, because they name `signups` and a declared `region` dimension and
        # the bundled jaffle tree declares no dimensions. Route, parameter names
        # and status are all still genuinely exercised.
        on_slice_fixture={
            "GET /metrics/signups/query?dimension=region",
            "POST /rca/signups/slices?dimension=region&reference_start=2024-02-05"
            "&reference_end=2024-03-03&analysis_start=2024-03-04&analysis_end=2024-03-10",
        },
        has_route_table=True,
    ),
    DocFile(
        # The MCP server's page. Its worked session runs over the MCP wire
        # protocol against the White Cube demo snapshots — replayed by the
        # section (c) fixture below, not the curl machinery (its two code
        # blocks are the connect snippets, not HTTP examples).
        "docs/mcp.md",
        yaml_blocks=0,
        parsable_yaml_blocks=0,
        skipped_yaml=[],
        curl_examples=0,
    ),
]

BY_NAME = {doc.name: doc for doc in DOCS}
README = BY_NAME["README.md"]
MCP_DOC = BY_NAME["mcp.md"]

# Parametrized over the parsable blocks only, rather than skipping the
# fragments at runtime: the sdist CI job audits every skip reason in the shipped
# suite against a fixed allow-list, and "this block is a fragment" is not one of
# the three shapes it accepts. The fragments are covered instead by the
# skip-list test and the block-count tripwire below, which is where the
# accounting belongs anyway.
ALL_PARSABLE_BLOCKS = [b for doc in DOCS for b in doc.parsable_blocks]
ALL_CURLS = [ex for doc in DOCS for ex in doc.curls]

SLICE_FIXTURE_TREE = """
provider:
  type: mock
metrics:
  - name: signups
    source: my.metrics.signups
    dimensions:
      region: customer__region
"""

# Replayed examples are expected to succeed; stated as a table so a deliberate
# 4xx example could be documented as one. Keyed by `CurlExample.key`.
EXPECTED_STATUS: dict = {}


# --------------------------------------------------------------------------
# (a) Every ```yaml block parses, cleanly
# --------------------------------------------------------------------------


def _stub_for(name: str) -> dict:
    return {"name": name, "source": f"docs_test.stubbed.{name}", "kind": _STUB_KIND}


def _referenced_elsewhere(metrics: list) -> list:
    """Names an excerpt leans on that it does not itself define — parents, the
    `denominator` a rate aggregates by, and the `weight` metric a rate
    dimension blends by (the same fact, in its older spelling)."""
    defined = {m["name"] for m in metrics}
    referenced = set()
    for m in metrics:
        referenced.update(m.get("parents") or [])
        if m.get("denominator"):
            referenced.add(m["denominator"])
        for spec in (m.get("dimensions") or {}).values():
            if isinstance(spec, dict) and spec.get("weight"):
                referenced.add(spec["weight"])
    return sorted(referenced - defined)


def _as_parsable_document(data):
    """-> (document, synthesized_names) or (None, None) for a fragment.

    Excerpts legitimately reference metrics defined elsewhere in the docs, so
    the missing ones are synthesized rather than invented *into* the page.
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
            {"provider": data["provider"], "metrics": [_stub_for("docs_probe")]},
            ["docs_probe (this block declares no metrics of its own)"],
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
        f"(each as {{source: docs_test.stubbed.<name>, kind: {_STUB_KIND}}}). "
        f"They are scaffolding, not documentation content — if the failure is about "
        f"one of them, this test is wrong rather than the docs."
    )


@pytest.mark.parametrize("block", ALL_PARSABLE_BLOCKS, ids=lambda b: b.id)
def test_documented_yaml_block_parses_without_warnings(block, caplog):
    """Fail on the parser's warnings as well as its exceptions: the flagship
    example used to parse successfully while emitting the `kind: rate` lint —
    a passing parse that told the reader their own tree was wrong."""
    doc, synthesized = _as_parsable_document(yaml.safe_load(block.body))
    note = _stub_note(synthesized)

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="breakdown"):
        try:
            Parser(yaml.safe_dump(doc, sort_keys=False))
        except Exception as exc:  # noqa: BLE001 — the doc's own error is the message
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


@pytest.mark.parametrize("doc", DOCS, ids=lambda d: d.name)
def test_yaml_skip_list_is_exactly_what_we_expect(doc):
    """A block that silently stops being a metric definition must fail here
    rather than vanish into "it's just a fragment"."""
    skipped = []
    for block in doc.yaml_blocks:
        data = yaml.safe_load(block.body)
        if _classify(data) == "fragment":
            key = ",".join(data) if isinstance(data, dict) else type(data).__name__
            skipped.append((key, block.line))

    assert sorted(k for k, _ in skipped) == doc.expected_skipped_yaml, (
        f"The set of {doc.name} YAML blocks skipped as non-metric fragments changed.\n"
        f"  found (key, line): {sorted(skipped)}\n"
        f"  expected keys:     {doc.expected_skipped_yaml}\n"
        "If a real example moved into this list it stopped looking like a metric "
        f"definition — fix {doc.name}, not this list."
    )


@pytest.mark.parametrize("doc", DOCS, ids=lambda d: d.name)
def test_doc_has_the_yaml_blocks_we_think_it_has(doc):
    """A tripwire: if the extractor stops seeing blocks (a fence style change, a
    deleted section, a page that moved its content elsewhere), every
    parametrized case above passes by not existing."""
    assert len(doc.yaml_blocks) == doc.expected_yaml_blocks, (
        f"expected {doc.expected_yaml_blocks} ```yaml blocks in {doc.name}, "
        f"found {len(doc.yaml_blocks)}"
    )
    assert len(doc.parsable_blocks) == doc.expected_parsable_yaml_blocks, (
        f"expected {doc.expected_parsable_yaml_blocks} parsable ```yaml blocks in "
        f"{doc.name}, found {len(doc.parsable_blocks)} — a documented example "
        "stopped being one, or a new one appeared unchecked"
    )


# --------------------------------------------------------------------------
# (b) Every documented HTTP example actually works
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def replayed(tmp_path_factory):
    """One pass over the app: boot the bundled tree, issue every replayable
    curl from every documentation file against it, then boot the slice
    fixture.

    Module-scoped and single-pass because `app` is a process-wide singleton (two
    TestClient lifespans must not overlap) and because on-demand ADVI fits are
    cached per fit window.
    """
    results = {}

    def issue(client, ex: CurlExample):
        kwargs = {"params": dict(parse_qsl(ex.query)), "headers": ex.headers}
        if ex.body is not None:
            kwargs["json"] = json.loads(ex.body)
        return client.request(ex.method, ex.path, **kwargs)

    demo = [
        ex
        for ex in ALL_CURLS
        if ex.name not in ex.doc.not_replayed and ex.name not in ex.doc.on_slice_fixture
    ]
    fixture = [ex for ex in ALL_CURLS if ex.name in ex.doc.on_slice_fixture]

    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("BREAKDOWN_TREE", str(DEMO_TREE))
        mp.setenv("BREAKDOWN_START_DATE", "2024-01-01")
        mp.setenv("BREAKDOWN_END_DATE", "2024-04-09")
        mp.delenv("BREAKDOWN_API_TOKEN", raising=False)
        mp.delenv("BREAKDOWN_REQUIRE_AUTH", raising=False)
        with TestClient(app) as client:
            for ex in demo:
                results[ex.key] = issue(client, ex)

    tree_file = tmp_path_factory.mktemp("docs") / "slice_fixture.yml"
    tree_file.write_text(SLICE_FIXTURE_TREE)
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("BREAKDOWN_TREE", str(tree_file))
        mp.setenv("BREAKDOWN_START_DATE", "2024-01-01")
        mp.setenv("BREAKDOWN_END_DATE", "2024-04-09")
        with TestClient(app) as client:
            for ex in fixture:
                results[ex.key] = issue(client, ex)

    return results


@pytest.mark.parametrize("doc", DOCS, ids=lambda d: d.name)
def test_doc_has_the_curl_examples_we_think_it_has(doc):
    assert len(doc.curls) == doc.expected_curl_examples, (
        f"expected {doc.expected_curl_examples} curl examples in {doc.name}, "
        f"found {len(doc.curls)}: {[e.where for e in doc.curls]}"
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
    for ex in ALL_CURLS:
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
    for ex in ALL_CURLS:
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
            "either the documentation is stale or the parameter was renamed."
        )


def test_replayed_curl_examples_return_the_documented_status(replayed):
    results = replayed
    failures = []
    for ex in ALL_CURLS:
        if ex.name in ex.doc.not_replayed:
            continue
        resp = results[ex.key]
        expected = EXPECTED_STATUS.get(ex.key, 200)
        if resp.status_code != expected:
            failures.append(
                f"{ex.where}: expected {expected}, got {resp.status_code} — {resp.text[:400]}"
            )
    assert not failures, "Documented curl examples that no longer work:\n" + "\n".join(failures)


@pytest.mark.parametrize("doc", DOCS, ids=lambda d: d.name)
def test_curl_skip_list_is_exactly_what_we_expect(doc):
    names = {ex.name for ex in doc.curls}
    assert not set(doc.not_replayed) - names, (
        f"{doc.name}'s not-replayed list names curl examples it no longer contains: "
        f"{sorted(set(doc.not_replayed) - names)}. Drop them, or the skip list "
        "protects nothing."
    )
    assert not doc.on_slice_fixture - names, (
        f"{doc.name}'s slice-fixture list names curl examples it no longer contains: "
        f"{sorted(doc.on_slice_fixture - names)}"
    )


# --------------------------------------------------------------------------
# The route table documents every route
# --------------------------------------------------------------------------

_ROUTE_DECORATOR = re.compile(
    r"^@(?:app|router)\.(get|post|put|patch|delete)\(\s*[\"']([^\"']+)[\"']", re.M
)


def _normalize(path: str) -> str:
    """`/trees/{tree_id}/load` and the docs' `/trees/{id}/load` are the same
    route; the placeholder's spelling is not part of the contract."""
    return re.sub(r"\{[^}]*\}", "{}", path)


def _route_table_doc() -> DocFile:
    """Whichever documentation file carries the route table, from `DOCS`.

    Read off the file list rather than hard-coded, because the table has
    already moved once (README -> docs/api-reference.md) and the assertion has
    to survive it moving again: retag the entry, and this follows.
    """
    carriers = [d for d in DOCS if d.has_route_table]
    assert len(carriers) == 1, (
        "Exactly one documentation file must carry the route table; "
        f"{[d.name for d in carriers]} claim to."
    )
    return carriers[0]


def _documented_paths(doc: DocFile) -> set:
    """Paths named in a table *cell* anywhere in the file.

    A whole cell, not a substring: scanning for backticked runs anywhere in the
    line makes prose like ``grains`/`kinds`` yield a phantom ``/`` entry, and
    this assertion then passes by accident — which is how it read on its first
    run. Scanning the whole file rather than one named section is what lets the
    table move within its page without silently emptying this set; the
    whole-cell rule is what keeps the scan honest while it does.
    """
    found = set()
    for line in doc.text.splitlines():
        if not line.lstrip().startswith("|"):
            continue
        for cell in line.strip().strip("|").split("|"):
            m = re.fullmatch(r"`(/[^`]*)`", cell.strip())
            if m:
                found.add(_normalize(m.group(1)))
    return found


def test_route_table_documents_every_route():
    """The assertion that would have caught `GET /metrics/{name}/query` being
    documented nowhere."""
    doc = _route_table_doc()
    declared = {_normalize(p) for _, p in _ROUTE_DECORATOR.findall(MAIN_PY)}
    missing = sorted(declared - _documented_paths(doc))
    assert not missing, (
        "Routes defined in breakdown/api/main.py but absent from the route "
        f"tables in {doc.name}: {missing}. A route nobody documents is a route "
        "nobody uses."
    )


# --------------------------------------------------------------------------
# (c) docs/mcp.md's worked session comes from a run we actually performed
# --------------------------------------------------------------------------

# The transcript lived in the README until 2026-08-20 and was pinned there as
# landing-page content. The guideline changed when README.md became
# human-owned (see AGENTS.md: agents do not edit it) and the MCP surface got
# its own page. The session now speaks the MCP wire protocol against the
# committed White Cube demo snapshots — the same hermetic setup as
# tests/test_white_cube_demo.py — so it exercises the tool layer itself
# (compaction, `how_to_read`, `sign_warnings`, `report_url`), not just the
# HTTP routes behind it. Skipped when demo/ is absent: it is repo-only,
# excluded from the sdist.
#
# Tolerances: tight (abs<=1e-3 in printed units) for anything read off the
# committed parquet through deterministic arithmetic; bands for posterior
# quantities (a seeded fit is not version-stable across PyMC/numpy bumps);
# and *structural* assertions — which row headlines, which flags are set —
# for the two known-issue beats. Those last are pinned exactly so that
# shipping roadmap 2.21 turns this section red and the doc's third finding
# gets rewritten, rather than silently describing behavior that no longer
# exists.

WC_DEMO = REPO / "demo"
WC_SNAPSHOTS = WC_DEMO / ".breakdown" / "snapshots"

# The story B windows from knowledge/demo_guided_tour.md — the planted
# professional churn spike, asked about from the churn-rate side.
SESSION_WINDOWS = {
    "reference_start": "2026-03-16",
    "reference_end": "2026-04-12",
    "analysis_start": "2026-05-11",
    "analysis_end": "2026-06-07",
}
_MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


@pytest.fixture(scope="module")
def mcp_session():
    """The five tool calls the worked session narrates, over the wire."""
    if not WC_SNAPSHOTS.is_dir() or not any(WC_SNAPSHOTS.iterdir()):
        pytest.skip("demo snapshots not present — demo/ is repo-only; run `make -C demo snapshots`")
    out = {}
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("BREAKDOWN_TREE", str(WC_DEMO / "white_cube_tree.yml"))
        mp.setenv("BREAKDOWN_START_DATE", "2024-06-01")
        mp.setenv("BREAKDOWN_END_DATE", "2026-07-30")
        mp.setenv("BREAKDOWN_SNAPSHOT_DIR", str(WC_SNAPSHOTS))
        mp.setenv("WHITE_CUBE_DBT_PROJECT", "/nonexistent/white-cube-has-no-provider")
        # The doc quotes the live demo's report_url; minting it needs the
        # public base the deployed instance uses.
        mp.setenv("BREAKDOWN_PUBLIC_URL", "https://white-cube-demo.fly.dev")
        mp.delenv("BREAKDOWN_API_TOKEN", raising=False)
        mp.delenv("BREAKDOWN_REQUIRE_AUTH", raising=False)
        mp.delenv("BREAKDOWN_REFRESH", raising=False)
        # base_url matters: the transport's DNS-rebinding protection only
        # admits localhost hosts.
        with TestClient(app, base_url="http://127.0.0.1:9090") as client:

            def call(name, arguments):
                resp = client.post(
                    "/mcp/",
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {"name": name, "arguments": arguments},
                    },
                    headers=_MCP_HEADERS,
                )
                assert resp.status_code == 200, resp.text
                result = resp.json()["result"]
                assert not result.get("isError"), result
                (text,) = [b["text"] for b in result["content"] if b["type"] == "text"]
                return json.loads(text)

            out["tree"] = call("get_tree", {})
            out["rca"] = call("run_rca", {"target": "customer_churn_rate", **SESSION_WINDOWS})
            out["plan"] = call(
                "slice_metric", {"name": "churned_mrr", "dimension": "plan", **SESSION_WINDOWS}
            )
            out["country_rate"] = call(
                "slice_metric",
                {"name": "customer_churn_rate", "dimension": "country", **SESSION_WINDOWS},
            )
            out["country_mrr"] = call(
                "slice_metric", {"name": "churned_mrr", "dimension": "country", **SESSION_WINDOWS}
            )
    return out


def mcp_doc_prints(value, spec="{:.1f}%", scale=100.0):
    """The doc must print exactly this *measured* figure — the same both-ways
    guard as the tour's `prints` helper in tests/test_white_cube_demo.py:
    engine drifts, the string vanishes, red; doc edited by hand, same."""
    printed = spec.format(value * scale).replace("\u2212", "-")
    text = MCP_DOC.text.replace("\u2212", "-")
    assert printed in text, (
        f"docs/mcp.md does not print {printed}, but the session produced it. "
        "Either the engine moved and the doc is stale, or the doc was edited "
        "without its pin."
    )


def test_mcp_session_headline_and_engagement_clearing(mcp_session):
    """Finding one: churn jumped, and the engagement theory is *cleared* —
    tiny contribution, unsure posterior, and a remainder that on a one-parent
    node is the conclusion."""
    tree = mcp_session["tree"]
    assert tree["date_start"] == "2024-06-01" and tree["date_end"] == "2026-07-30"
    assert len(tree["metrics"]) == 23
    (ccr_meta,) = [m for m in tree["metrics"] if m["name"] == "customer_churn_rate"]
    assert ccr_meta["parents"] == ["member_activity_rate"]

    node = mcp_session["rca"]["nodes"]["customer_churn_rate"]
    assert node["status"] == "ok"
    # "0.91% -> 1.23%, +34.2%"
    assert node["baseline"] == pytest.approx(0.00913, abs=2e-4)
    assert node["actual"] == pytest.approx(0.01226, abs=2e-4)
    assert node["relative_change"] == pytest.approx(0.342, abs=5e-3)
    mcp_doc_prints(node["baseline"], "{:.2f}%")
    mcp_doc_prints(node["actual"], "{:.2f}%")
    mcp_doc_prints(node["relative_change"], "{:.1f}%")

    (eng,) = node["contributions"]
    assert eng["parent"] == "member_activity_rate"
    # "its contribution is 2.9% of the gap with an interval crossing zero"
    assert eng["share_of_gap"] == pytest.approx(0.029, abs=0.02)
    mcp_doc_prints(eng["share_of_gap"], "{:.1f}%")
    assert eng["ci_95"][0] < 0 < eng["ci_95"][1]
    assert eng["prob_same_direction"] == pytest.approx(0.65, abs=0.1)

    # "96% of the gap lands in unexplained (status measured)" — the finding.
    assert node["unexplained_status"] == "measured"
    unexplained_share = node["unexplained"] / node["gap"]
    assert unexplained_share > 0.9
    mcp_doc_prints(unexplained_share, "{:.0f}%")

    # "member activity barely moved (95.8% -> 96.2%)"
    mar = mcp_session["rca"]["nodes"]["member_activity_rate"]
    assert abs(mar["relative_change"]) < 0.02
    mcp_doc_prints(mar["baseline"], "{:.1f}%")
    mcp_doc_prints(mar["actual"], "{:.1f}%")


def test_mcp_session_carries_the_sign_warning(mcp_session):
    """The narration's 'second, independent reason' is a payload field: the
    fitted engagement edge contradicts its declared direction and the MCP
    response says so rather than compacting the warning away. If this fit
    ever agrees with its declaration, the doc's caveat is stale — rewrite the
    finding, don't delete the assertion."""
    node = mcp_session["rca"]["nodes"]["customer_churn_rate"]
    warnings = node.get("sign_warnings") or []
    assert warnings, "the doc says the payload carries sign_warnings on this fit"
    assert any("member_activity_rate" in w for w in warnings)


def test_mcp_session_plan_slice_localizes_professional(mcp_session):
    """Finding two: a pricing-tier story, with the verdict published."""
    s = mcp_session["plan"]
    assert s["localized"] is True
    top = s["slices"][0]
    assert top["value"] == "professional"
    assert top["share_of_gap"] == pytest.approx(1.006, abs=1e-3)
    assert top["baseline_share"] == pytest.approx(0.440, abs=1e-3)
    assert top["prob_concentrated"] > 0.99
    mcp_doc_prints(top["share_of_gap"], "{:.1f}%")
    mcp_doc_prints(top["baseline_share"], "{:.1f}%")


def test_mcp_session_known_issue_2_21_the_roll_up_headlines_the_verdict(mcp_session):
    """Finding three's known-issue half, pinned as a defect the way
    tests/test_white_cube_demo.py pins the churn_arpu colour gap: this goes
    red when roadmap 2.21 ships a long-tail verdict vocabulary, at which
    point docs/mcp.md's third finding must be rewritten to match."""
    s = mcp_session["country_rate"]
    assert s["localized"] is True, (
        "docs/mcp.md narrates a `localized: true` verdict headlined by the "
        "__other__ roll-up. If this is no longer true, roadmap 2.21 likely "
        "shipped — rewrite the doc's third finding."
    )
    assert s["localization_threshold"] == pytest.approx(0.25)
    top = s["slices"][0]
    assert top["value"] == "__other__"
    excess_share = abs(top["excess"] / s["gap"])
    assert excess_share == pytest.approx(0.264, abs=0.01)
    mcp_doc_prints(excess_share, "{:.1f}%")
    # "no named country clears the bar (the largest, BR, carries 13%)"
    named = [r for r in s["slices"] if r["value"] != "__other__"]
    named_excess = {r["value"]: abs(r["excess"] / s["gap"]) for r in named}
    assert max(named_excess.values()) < s["localization_threshold"]
    assert max(named_excess, key=named_excess.get) == "BR"
    mcp_doc_prints(named_excess["BR"], "{:.0f}%")


def test_mcp_session_country_mrr_cross_check_is_not_localized(mcp_session):
    """Finding three's cross-check: the dollars-side country slice declines,
    which is what entitles the narration to 'a tier, not a geography'."""
    s = mcp_session["country_mrr"]
    assert s["localized"] is False
    assert sum(1 for r in s["slices"] if r["noise_level"]) == 7
    assert len(s["slices"]) == 9


def test_mcp_doc_report_url_is_the_link_the_server_actually_mints(mcp_session):
    """The session closes on a deep link into the live demo and claims it
    replays the analysis; the link printed must be the one the server mints
    (tree prefix, window params and all)."""
    url = mcp_session["rca"]["report_url"]
    assert url.startswith("https://white-cube-demo.fly.dev/ui/#tree=white_cube_tree")
    assert url in MCP_DOC.text, (
        f"docs/mcp.md does not print the report_url the server mints:\n  {url}"
    )


def test_mcp_doc_does_not_call_the_session_an_unedited_transcript():
    """Its figures are regenerated by the fixture above and its narration is
    written prose, so the label cannot claim a captured model response."""
    section = MCP_DOC.text[MCP_DOC.text.index("## A worked session") :]
    section = section[: section.index("\n## ")].lower()
    for forbidden in ("unedited", "verbatim"):
        assert forbidden not in section, (
            f"The worked session calls itself {forbidden!r}. Its figures are "
            "regenerated by a run this module performs and its narration is "
            "written prose — the label has to say so."
        )


# --- Markdown tables that actually render -------------------------------------


def _markdown_files():
    """Every prose document in the repo, wherever it lives.

    `knowledge/` and `docs/` are not both present in every artifact — the sdist
    excludes `knowledge/` — so this globs what exists rather than listing paths
    that must.
    """
    found = []
    for pat in ("README.md", "AGENTS.md", "CHANGELOG.md", "docs/**/*.md", "knowledge/*.md"):
        found += sorted(REPO.glob(pat))
    return [p for p in found if p.is_file()]


def _tables(lines):
    """Yield (header_index, rows) for each pipe table, and the blank lines that
    interrupt one."""
    tables, splits = [], []
    i = 0
    while i < len(lines):
        if lines[i].lstrip().startswith("|---"):
            header = i - 1
            rows, j = [], i + 1
            while j < len(lines):
                if lines[j].lstrip().startswith("| "):
                    rows.append(j)
                    j += 1
                    continue
                if lines[j].strip() == "":
                    k = j
                    while k < len(lines) and lines[k].strip() == "":
                        k += 1
                    if k < len(lines) and lines[k].lstrip().startswith("| "):
                        splits.append(j)
                        j = k
                        continue
                break
            tables.append((header, rows))
            i = j
            continue
        i += 1
    return tables, splits


def _cells(line):
    """Structural cell count: a `\\|` inside a cell is content, not a separator."""
    return line.count("|") - line.count("\\|")


@pytest.mark.parametrize("path", _markdown_files(), ids=lambda p: p.name)
def test_markdown_tables_render_as_tables(path):
    """A table that is correct in source and broken on GitHub.

    Two ways to break one, both of which have shipped here. A **blank line**
    inside a table ends it, so every row after the gap renders as literal pipe
    text — that hit `docs/ai-context/frontend-ui.md` and, for a while, four
    tables in the roadmap including the Horizon 0 rows. An **unescaped `|`**
    inside a cell splits that row into extra cells — `min(|s_p|, 1)` and
    `up_is_good|down_is_good` each did it, silently turning a 5-cell row into a
    17- and a 7-cell one.

    Neither shows up in a diff, in ruff, or in any test that reads the prose.
    This is the third time the second one has been fixed by hand, which is the
    bar this file exists to enforce: anything you can assert, assert.
    """
    lines = path.read_text().split("\n")
    tables, splits = _tables(lines)

    assert not splits, (
        f"{path.name}: blank line(s) inside a markdown table at line(s) "
        f"{[n + 1 for n in splits]}. A blank line ends the table, so every row "
        "after it renders as literal pipe text."
    )

    ragged = []
    for header, rows in tables:
        want = _cells(lines[header])
        ragged += [
            (n + 1, _cells(lines[n]))
            for n in rows
            if _cells(lines[n]) != want and lines[n].strip() != ""
        ]
    assert not ragged, (
        f"{path.name}: row(s) whose structural cell count differs from their "
        f"header — {ragged}. Escape any `|` that is content, as `\\|`."
    )
