"""Fórmulas versionadas do LaBeer. Sem leitura de banco ou dependências de UI."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext


FORMULA_VERSION = "1.0"
INPUT_LABELS = {
    "alcohol": "Álcool (% v/v)",
    "apparent_extract": "Extrato aparente (°P)",
    "brix": "Leitura (°Brix)",
}
NUMERIC = re.compile(r"[+-]?(?:[0-9]+(?:[.,][0-9]*)?|[.,][0-9]+)(?:[eE][+-]?[0-9]+)?")


@dataclass(frozen=True)
class Formula:
    key: str
    intercept: Decimal
    terms: tuple[tuple[str, Decimal], ...]
    source: str
    version: str = FORMULA_VERSION


FORMULAS = (
    Formula("real_extract", Decimal("0.13004"),
            (("alcohol", Decimal("0.338825")), ("apparent_extract", Decimal("0.980571"))),
            "Calc_Extrato_Real!B2"),
    Formula("original_extract", Decimal("-0.0602441"),
            (("alcohol", Decimal("1.919014")), ("apparent_extract", Decimal("0.928576"))),
            "Calc_Extrato_Original!B2"),
    Formula("calories", Decimal("0.72"),
            (("alcohol", Decimal("6.898")), ("apparent_extract", Decimal("3.88"))),
            "Calc_Calorias!B2"),
    Formula("carbohydrates", Decimal("1.03"),
            (("brix", Decimal("-0.202")), ("apparent_extract", Decimal("0.739"))),
            "Calc_Carb!C2"),
)


@dataclass(frozen=True)
class Calculation:
    formula: Formula
    value: Decimal | None
    missing: tuple[str, ...] = ()


def parse_input(field: str, raw: Decimal | int | float | str | None) -> Decimal | None:
    """Converte entradas laboratoriais sem interpretar resultados censurados como números."""
    text = "" if raw is None else str(raw).strip()
    if not text:
        return None
    if len(text) > 90 or not NUMERIC.fullmatch(text):
        raise ValueError("Informe um número com vírgula ou ponto decimal, sem a unidade.")
    try:
        value = Decimal(text.replace(",", "."))
    except InvalidOperation:
        raise ValueError("Valor numérico fora da precisão aceita.") from None
    if not value.is_finite() or not -80 <= value.as_tuple().exponent <= 80:
        raise ValueError("Valor numérico fora da precisão aceita.")
    if field in ("alcohol", "brix") and not Decimal(0) <= value <= Decimal(100):
        raise ValueError("Use um valor numérico entre 0 e 100 na unidade indicada.")
    # A própria planilha contém extratos aparentes negativos. Não os truncar.
    if field == "apparent_extract" and value.copy_abs() > Decimal(100):
        raise ValueError("Use um valor numérico entre −100 e 100 °P.")
    return value


def calculate(inputs: Mapping[str, Decimal | None]) -> dict[str, Calculation]:
    """Calcula só o que tem entradas completas; nunca preenche uma lacuna com zero."""
    values = {k: parse_input(k, inputs.get(k)) for k in INPUT_LABELS}
    result = {}
    with localcontext() as ctx:
        ctx.prec = 100
        for formula in FORMULAS:
            missing = tuple(name for name, _ in formula.terms if values[name] is None)
            if missing:
                result[formula.key] = Calculation(formula, None, missing)
                continue
            value = formula.intercept
            for name, coefficient in formula.terms:
                value += coefficient * values[name]
            result[formula.key] = Calculation(formula, value)
    return result


def decimal_text(value: Decimal | None) -> str:
    if value is None:
        return ""
    number = format(value, "f")
    if "." in number:
        number = number.rstrip("0").rstrip(".")
    return number.replace(".", ",")
