"""Deterministic checks for a deliberately small grammar of numeric exercises."""

import re
from collections.abc import Iterator
from decimal import Decimal, DecimalException, Inexact, localcontext

from .contracts import RecognizedTask, TaskKind

NUMBER = r"[0-9]{1,12}(?:[.,][0-9]{1,12})?"
TOKEN = re.compile(rf"{NUMBER}|[+*/()-]")
ARITHMETIC_PREFIX = re.compile(r"^(?:вычислите|вычисли|найдите значение|evaluate|calculate)\s*:?\s*", re.I)
COMPARISON_PREFIX = re.compile(r"^(?:сравните|сравни|compare)\s*:?\s*", re.I)
MEASUREMENT_PREFIX = re.compile(r"^(?:переведите|переведи|convert)\s*:?\s*", re.I)
BLANK = r"(?:\?+|_+|□|\[\s*\])"

UNIT_GROUPS = {
    "length": {"мм": "0.001", "mm": "0.001", "см": "0.01", "cm": "0.01",
               "дм": "0.1", "dm": "0.1", "м": "1", "m": "1", "км": "1000", "km": "1000"},
    "mass": {"мг": "0.001", "mg": "0.001", "г": "1", "g": "1",
             "кг": "1000", "kg": "1000", "т": "1000000", "t": "1000000"},
    "time": {"с": "1", "s": "1", "сек": "1", "мин": "60", "min": "60",
             "ч": "3600", "h": "3600"},
}
UNITS = {
    unit: (dimension, Decimal(factor))
    for dimension, group in UNIT_GROUPS.items()
    for unit, factor in group.items()
}
UNIT = r"[a-zа-яё]{1,5}"
MEASUREMENT = re.compile(
    rf"([+-]?{NUMBER})\s*({UNIT})\s*(?:=\s*{BLANK}|\bв\b|\bto\b)\s*({UNIT})",
    re.I,
)


def _number(value: str) -> Decimal:
    return Decimal(value.replace(",", "."))


def _tokens(expression: str) -> Iterator[str]:
    position = 0
    while position < len(expression):
        if expression[position].isspace():
            position += 1
            continue
        token = TOKEN.match(expression, position)
        if token is None:
            raise ValueError
        yield token[0]
        position = token.end()


class _Arithmetic:
    def __init__(self, expression: str):
        self.tokens = iter(_tokens(expression))
        self.current = next(self.tokens, None)

    def take(self) -> str:
        if self.current is None:
            raise ValueError
        token = self.current
        self.current = next(self.tokens, None)
        return token

    def atom(self, depth: int) -> Decimal:
        if depth > 16:
            raise ValueError
        token = self.take()
        if token in {"+", "-"}:
            value = self.atom(depth + 1)
            return -value if token == "-" else value
        if token == "(":
            result = self.expression(depth + 1)
            if self.take() != ")":
                raise ValueError
            return result
        return _number(token)

    def term(self, depth: int) -> Decimal:
        result = self.atom(depth)
        while self.current in {"*", "/"}:
            op = self.take()
            right = self.atom(depth)
            result = result * right if op == "*" else result / right
        return result

    def expression(self, depth: int = 0) -> Decimal:
        result = self.term(depth)
        while self.current in {"+", "-"}:
            op = self.take()
            right = self.term(depth)
            result = result + right if op == "+" else result - right
        return result

    def calculate(self) -> Decimal:
        result = self.expression()
        if self.current is not None:
            raise ValueError
        return result


def _text(value: str) -> str:
    return value.translate(str.maketrans({"−": "-", "×": "*", "·": "*", "÷": "/", "≤": "<=", "≥": ">="}))


def _format(value: Decimal) -> str:
    if not value:
        return "0"
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _arithmetic(condition: str, answer: str) -> tuple[bool, str] | None:
    expression = ARITHMETIC_PREFIX.sub("", condition)
    expression = re.sub(rf"\s*=\s*{BLANK}?\s*$", "", expression)
    expression = expression.replace(":", "/")
    if len(expression) > 120 or not re.fullmatch(rf"[+-]?{NUMBER}", answer):
        return None
    if not re.search(r"[+*/-]", expression[1:]):
        return None
    expected = _Arithmetic(expression).calculate()
    return expected == _number(answer), _format(expected)


def _comparison(condition: str, answer: str) -> tuple[bool, str] | None:
    text = COMPARISON_PREFIX.sub("", condition)
    match = re.fullmatch(rf"([+-]?{NUMBER})\s*(?:{BLANK}|\bи\b|\band\b|,\s+|;)\s*([+-]?{NUMBER})", text, re.I)
    if match is None or answer not in {"<", ">", "="}:
        return None
    left, right = map(_number, match.groups())
    expected = "<" if left < right else ">" if left > right else "="
    return answer == expected, expected


def _measurement(condition: str, answer: str) -> tuple[bool, str] | None:
    target = MEASUREMENT.fullmatch(MEASUREMENT_PREFIX.sub("", condition))
    response = re.fullmatch(rf"([+-]?{NUMBER})\s*({UNIT})?", answer, re.I)
    if target is None:
        return _compare_measurements(condition, answer)
    if response is None:
        return None
    amount, from_unit, to_unit = target.groups()
    given, given_unit = response.groups()
    from_unit, to_unit = from_unit.lower(), to_unit.lower()
    if from_unit not in UNITS or to_unit not in UNITS:
        return None
    dimension, source_factor = UNITS[from_unit]
    to_dimension, target_factor = UNITS[to_unit]
    if dimension != to_dimension:
        return None
    expected = _number(amount) * source_factor / target_factor
    correct_unit = given_unit is None or UNITS.get(given_unit.lower()) == (dimension, target_factor)
    return correct_unit and _number(given) == expected, f"{_format(expected)} {to_unit}"


def _compare_measurements(condition: str, answer: str) -> tuple[bool, str] | None:
    if answer not in {"<", ">", "="}:
        return None
    comparison = re.fullmatch(
        rf"([+-]?{NUMBER})\s*({UNIT})\s*(?:{BLANK}|\bи\b|\band\b|,\s+|;)\s*([+-]?{NUMBER})\s*({UNIT})",
        COMPARISON_PREFIX.sub("", condition),
        re.I,
    )
    if comparison is None:
        return None
    left, left_unit, right, right_unit = comparison.groups()
    lhs, rhs = UNITS.get(left_unit.lower()), UNITS.get(right_unit.lower())
    if lhs is None or rhs is None or lhs[0] != rhs[0]:
        return None
    left_value, right_value = _number(left) * lhs[1], _number(right) * rhs[1]
    expected = "<" if left_value < right_value else ">" if left_value > right_value else "="
    return answer == expected, expected


CHECKS = {
    TaskKind.ARITHMETIC: _arithmetic,
    TaskKind.NUMERIC_COMPARISON: _comparison,
    TaskKind.MEASUREMENT: _measurement,
}


def formal_check(task: RecognizedTask) -> tuple[bool, str] | None:
    if not task.condition or not task.answer:
        return None
    if task.kind == TaskKind.UNKNOWN:
        # A teacher may supply a previously missing condition during a targeted recheck.
        checks = tuple(CHECKS.values())
    elif task.kind in CHECKS:
        checks = (CHECKS[task.kind],)
    else:
        return None

    condition, answer = _text(task.condition), _text(task.answer)
    with localcontext() as context:
        context.prec = 128
        context.traps[Inexact] = True
        for check in checks:
            try:
                result = check(condition, answer)
            except (DecimalException, ValueError):
                continue
            if result is not None:
                return result
    return None


