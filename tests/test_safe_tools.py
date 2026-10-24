"""Resource and type boundaries for model-controlled calculator input."""

import ast
import math
from pathlib import Path

import pytest

from tankai.core.tools import CalculatorTool, TextStatsTool


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("2+2*3", 8),
        ("sqrt(16)", 4.0),
        ("-2 + +3", 1),
        ("2**10", 1024),
        ("2**-3", 0.125),
        ("7%3", 1),
        ("abs(-12)", 12),
        ("round(3.14159, 3)", 3.142),
        ("round(1234, -2)", 1200),
        ("round(1.9)", 2),
        ("log(8, 2)", 3.0),
        ("log(e)", 1.0),
        ("sin(pi/2) + cos(0)", 2.0),
    ],
)
def test_normal_calculator_expressions(expression, expected):
    assert CalculatorTool().run(expression) == f"Ergebnis: {expected}"


def test_expr_alias_remains_available():
    assert CalculatorTool().run(expr="3*7+1") == "Ergebnis: 22"


@pytest.mark.parametrize(
    "expression",
    [
        "'a' * 10**9", "'a'", "b'abc'", "True + 1", "False", "None",
        "[1] * 1000", "(1, 2)", "{'x': 1}", "{1, 2}", "1j", "(-1)**0.5",
        "1e309", "1e101", "1e100 * 2", "1/0", "sqrt(-1)", "log(0)",
        "1 << 100", "1 // 2", "2 < 3", "not 1", "~1", "__import__('os')",
        "math.sqrt(4)", "(lambda: 1)()", "sqrt(x=4)", "round(1, ndigits=2)",
        "sqrt()", "sqrt(1, 2)", "sin(1, 2)", "cos(1, 2)", "abs(1, 2)",
        "round()", "round(1, 2, 3)", "log()", "log(1, 2, 3)",
        "round(1.2, 1.0)", "round(1.2, True)", "round(1.2, 101)",
        "round(1.2, -101)", "round(1.2, 10**9)", "2**1001", "2**-1001",
        "2**(2**1000)", "10**1000", "(2**1000)*(2**1000)",
    ],
)
def test_unsafe_or_invalid_expressions_return_bounded_errors(expression):
    result = CalculatorTool().run(expression)
    assert result.startswith("Rechenfehler: ")
    assert len(result) < 1200


@pytest.mark.parametrize("value", [42, True, [], {}, object()])
def test_non_text_input_is_rejected(value):
    assert CalculatorTool().run(value).startswith("Rechenfehler:")


@pytest.mark.parametrize("value", [True, False, "text", b"text", None, complex(1, 2), math.inf, math.nan])
def test_direct_constant_evaluation_rejects_non_real_or_nonfinite_values(value):
    with pytest.raises(ValueError):
        CalculatorTool()._eval(ast.Constant(value=value))


def test_integer_literal_is_bounded():
    assert CalculatorTool().run(str(1 << 1024)).startswith("Rechenfehler:")
    assert CalculatorTool().run("2**1000") == f"Ergebnis: {2**1000}"


def test_large_power_is_rejected_before_computation(monkeypatch):
    calls = []

    def forbidden_power(*args):
        calls.append(args)
        raise AssertionError("Unbounded power was evaluated")

    monkeypatch.setattr("tankai.core.tools.operator.pow", forbidden_power)
    calculator = CalculatorTool()
    assert "Exponent" in calculator.run("2**1000000000")
    assert "Potenz" in calculator.run("10**1000")
    assert calls == []


def test_function_arity_checked_before_arguments_are_evaluated(monkeypatch):
    def forbidden_power(*args):
        raise AssertionError("Invalid function call evaluated its arguments")

    monkeypatch.setattr("tankai.core.tools.operator.pow", forbidden_power)
    assert "Funktionsargumente" in CalculatorTool().run("sqrt(2**10, 3)")


def test_expression_length_rejected_before_parsing(monkeypatch):
    def forbidden_parse(*args, **kwargs):
        raise AssertionError("Oversized input was parsed")

    monkeypatch.setattr("tankai.core.tools.ast.parse", forbidden_parse)
    assert "zu lang" in CalculatorTool().run("1" * 1025)


def test_ast_depth_is_bounded():
    assert "zu komplex" in CalculatorTool().run("-" * 30 + "1")


def test_ast_node_count_is_bounded_before_evaluation():
    # A balanced tree avoids the depth bound, exercising the separate node cap.
    expression = "1"
    for _ in range(6):
        expression = f"({expression}+{expression})"
    assert len(expression) < CalculatorTool.MAX_EXPRESSION_LENGTH
    assert "zu komplex" in CalculatorTool().run(expression)


def test_text_stats_counts_all_whitespace():
    assert TextStatsTool().run("a b\tc\nd\u00a0e") == (
        "Zeichen: 9 | Wörter: 5 | Zeilen: 2 | Ohne Leerzeichen: 5"
    )
    assert TextStatsTool().run(" ") == (
        "Zeichen: 1 | Wörter: 0 | Zeilen: 1 | Ohne Leerzeichen: 0"
    )


def test_tools_source_is_python_311_syntax_compatible():
    source = Path(__file__).parents[1] / "tankai" / "core" / "tools.py"
    ast.parse(source.read_text(encoding="utf-8"), feature_version=(3, 11))
