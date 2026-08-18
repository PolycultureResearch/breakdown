"""Shared helpers for validating and evaluating metric formulas.

Formulas are restricted Python arithmetic expressions: only +, -, *, /, **,
unary +/-, numeric constants, and bare names are allowed. Everything else
(calls, attributes, subscripts, comprehensions) is rejected before eval.
"""

import ast
from typing import Dict, Set

import numpy as np

ALLOWED_NODES = (
    ast.Expression,
    ast.BinOp,
    ast.Name,
    ast.Constant,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.Pow,
    ast.UnaryOp,
    ast.USub,
    ast.UAdd,
    ast.Load,  # context node on Name nodes
)


def _parse(formula: str) -> ast.Expression:
    try:
        return ast.parse(formula, mode="eval")
    except SyntaxError as e:
        raise ValueError(f"Invalid formula syntax: {e}") from e


def validate_formula(formula: str) -> None:
    tree = _parse(formula)
    for node in ast.walk(tree):
        if not isinstance(node, ALLOWED_NODES):
            raise ValueError(f"Formula contains unsupported operation: {type(node).__name__}")


def referenced_names(formula: str) -> Set[str]:
    return {node.id for node in ast.walk(_parse(formula)) if isinstance(node, ast.Name)}


def divisor_expressions(formula: str) -> list:
    """Source text of every sub-expression `formula` divides by.

    Cold start refuses a scenario whose divisor draws cross zero (roadmap C7):
    the Monte-Carlo mean of such a ratio does not exist, so the published
    centre would be an artifact of the seed. Finding the divisors is this
    function's half of that check; evaluating them is the caller's, because
    only the caller holds the draws. A constant negative exponent is a
    division in costume (`x ** -1`), so the base of one counts too.
    """
    out = []
    for node in ast.walk(_parse(formula)):
        if not isinstance(node, ast.BinOp):
            continue
        if isinstance(node.op, ast.Div):
            out.append(ast.unparse(node.right))
        elif isinstance(node.op, ast.Pow):
            try:
                exponent = ast.literal_eval(node.right)
            except (ValueError, SyntaxError):
                continue
            if isinstance(exponent, (int, float)) and exponent < 0:
                out.append(ast.unparse(node.left))
    return out


def eval_formula(formula: str, values: Dict[str, np.ndarray]) -> np.ndarray:
    """Evaluate a validated formula over per-metric arrays.

    A zero denominator yields numpy's own `inf`/`nan`; callers decide what a
    non-finite value means for them (`shapley_attribution` refuses to attribute
    one, naming the offending series).

    The `errstate` block is load-bearing, not cosmetic. numpy reports
    divide-by-zero and invalid-value conditions through Python's *warnings*
    machinery, which resolves `__import__` from the **calling frame's globals**
    — and those globals are deliberately `{"__builtins__": {}}` here, which is
    what makes `eval` safe alongside the AST allow-list above. So the very
    first zero denominator used to die with `KeyError: '__import__'` instead of
    producing `inf`. Silencing the conditions means that path is never entered.
    Do **not** "fix" a future recurrence by putting `__import__` (or any other
    builtin) back into the eval globals — that reopens the sandbox. Keep the
    allow-list and the empty builtins exactly as they are.
    """
    validate_formula(formula)
    with np.errstate(divide="ignore", invalid="ignore", over="ignore", under="ignore"):
        return eval(formula, {"__builtins__": {}}, values)  # noqa: S307
