"""Importação única da planilha LaBeer, sem escrever na origem."""

from __future__ import annotations

import json
import os
import re
import tempfile
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from sqlalchemy import URL, create_engine, select, text
from sqlalchemy.orm import Session

from .db import Base
from .models import AnalysisExecution, AnalysisParameter, AuditEvent, CurrentResult, RequestedAnalysis, Sample, fq_code
from .services import PARAMETER_SEED, deadline_for, seed_parameters


SAMPLE_HEADER = (
    "Código FQ", "Código FQ", "Cervejaria", "Nome da Cerveja", "Estilo",
    "Lote", "Data de Entrega", "Observações", "Status do laudo",
)
ANALYSIS_HEADER = (
    "Código FQ", "Parâmetro", "Resultado", "Unidade", "Método",
    "Data da Análise", "Resultado consolidado", "Status", "Incluir no laudo?",
)
SUPPORT_SHEETS = {
    "Calc_Alcool": ("alcohol", 1, "Álcool (% v/v)", 3),
    "Calc_Extrato_Aparente": ("apparent_extract", 1, "Extrato Aparente (%)", 2),
    "Calc_Extrato_Real": ("real_extract", 1, "Extrato Real (%)", 3),
    "Calc_Extrato_Original": ("original_extract", 1, "Extrato Original (%)", 3),
    "Calc_Calorias": ("calories", 1, "Calorias (kcal/100 mL)", 3),
    "Calc_IBU": ("ibu", 5, "IBU Médio", 7),
    "Calc_ASBC": ("color", 2, "Cor (ASBC)", 4),
    "Calc_pH": ("ph", 1, "pH", 3),
    "Calc_densidade": ("density", 1, "Densidade (g/cm3)", 3),
    "Calc_Carb": ("carbohydrates", 2, "Carboidratos", 4),
    "Calc_Gluten": ("gluten", 1, "Resultado final (ppm)", 4),
    "Calc_Shelf_Life": ("shelf_life", 1, "Shelf Life (dias)", 4),
}
WIDTHS = {"Amostras": 11, "Analises": 9, **{k: v[3] for k, v in SUPPORT_SHEETS.items()}}
LEGACY_REFERENCE = "spreadsheet_reference"
FQ = re.compile(r"FQ\s*0*([1-9][0-9]{0,8})", re.IGNORECASE)
LEGACY_CODE = re.compile(r"B\s*[0-9]+", re.IGNORECASE)
NUMBER = re.compile(r"[+-]?\d+(?:[,.]\d+)?(?:[eE][+-]?\d+)?")
QUALIFIED = re.compile(r"(?:[<>]=?|[≤≥])\s*[+-]?\d+(?:[,.]\d+)?(?:\s*(?:ppm|dias|g/100\s*mL))?", re.I)
CLOSED = "Encerrada sem resultado"


def clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


def key(value: Any) -> str:
    no_accents = "".join(c for c in unicodedata.normalize("NFKD", clean(value)) if not unicodedata.combining(c))
    return " ".join(no_accents.casefold().split())


PARAMETER_NAMES = {
    **{key(name): code for code, name, *_ in PARAMETER_SEED},
    "amargor (ibu)": "ibu",
    "cor (asbc)": "color",
}


def fq_number(value: Any) -> int | None:
    match = FQ.fullmatch(clean(value))
    return int(match[1]) if match else None


def read_date(value: Any, epoch: date) -> date | None:
    if clean(value) == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        raw = Decimal(str(value))
        if raw.is_finite() and 1 <= raw < 100_000:
            return epoch + timedelta(days=int(raw))
    if isinstance(value, str):
        for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
            try:
                return datetime.strptime(value.strip(), fmt).date()
            except ValueError:
                pass
    raise ValueError("data inválida")


@dataclass(frozen=True)
class Result:
    numeric: Decimal | None = None
    textual: str | None = None

    def as_text(self) -> str:
        return format(self.numeric, "f").replace(".", ",") if self.numeric is not None else self.textual or "—"

    def identity(self) -> Decimal | str:
        if self.numeric is not None:
            return self.numeric
        if re.sub(r"[.\s]", "", (self.textual or "").casefold()) == "nd":
            return "n.d."
        return re.sub(r"\s+", "", (self.textual or "").casefold()).replace(",", ".")


def read_result(value: Any) -> Result | None:
    s = clean(value)
    if not s:
        return None
    if NUMBER.fullmatch(s):
        try:
            n = Decimal(s.replace(",", "."))
            if n.is_finite() and len(n.as_tuple().digits) <= 80 and -80 <= n.as_tuple().exponent <= 80:
                return Result(numeric=n)
        except InvalidOperation:
            pass
    elif QUALIFIED.fullmatch(s) or re.sub(r"[.\s]", "", s.casefold()) == "nd":
        return Result(textual=s)
    raise ValueError("resultado inválido ou erro da planilha")


@dataclass(frozen=True)
class Snapshot:
    name: str
    sheets: dict[str, list[list[Any]]]
    epoch: date = date(1899, 12, 30)
    uncached_status: frozenset[int] = frozenset()


def read_xlsx(path: Path) -> Snapshot:
    if path.suffix.lower() != ".xlsx":
        raise ValueError("Use a exportação .xlsx da planilha oficial.")
    from openpyxl import load_workbook

    with path.open("rb") as source:
        values = load_workbook(source, read_only=True, data_only=True, keep_links=False)
        try:
            known = {key(name): name for name in WIDTHS}
            sheets = {
                known[key(sheet.title)]: [list(row) for row in sheet.iter_rows(max_col=WIDTHS[known[key(sheet.title)]], values_only=True)]
                for sheet in values.worksheets if key(sheet.title) in known
            }
            epoch = values.epoch.date()
        finally:
            values.close()

        # Fórmulas de status H não devem estar vazias em linhas com análise em uso.
        # Isso detecta uma exportação sem resultados salvos sem recalcular a planilha.
        uncached = set()
        if "Analises" in sheets:
            source.seek(0)
            formulas = load_workbook(source, read_only=True, data_only=False, keep_links=False)
            try:
                sheet = next(s for s in formulas.worksheets if known.get(key(s.title)) == "Analises")
                for row_no, (status,) in enumerate(sheet.iter_rows(min_col=8, max_col=8), 1):
                    if status.data_type == "f" and (
                        row_no > len(sheets["Analises"]) or not clean(cell(sheets["Analises"][row_no - 1], 7))
                    ):
                        uncached.add(row_no)
            finally:
                formulas.close()
    return Snapshot(path.name, sheets, epoch, frozenset(uncached))


def cell(row: list[Any], index: int) -> Any:
    return row[index] if index < len(row) else None


@dataclass(frozen=True)
class SourceRow:
    sheet: str
    number: int
    cells: list[Any]

    @property
    def location(self) -> str:
        return f"{self.sheet}:{self.number}"

    def details(self) -> dict:
        return {"aba": self.sheet, "linha": self.number, "codigo_origem": self.cells[0], "celulas": self.cells}


@dataclass(frozen=True)
class LegacyReference:
    fq: int
    parameter: str
    source: SourceRow
    result: Result | None


@dataclass(frozen=True)
class AnalysisRow:
    source: SourceRow
    result: Result | None
    date: date | None
    status: str
    include: bool | None
    value_column: str | None


@dataclass
class Analysis:
    parameter: str
    rows: list[AnalysisRow]
    executions: list[list[AnalysisRow]]
    selected: int | None
    status: str
    include: bool | None

    @property
    def current(self) -> Result | None:
        if self.selected is None:
            return None
        return self.executions[self.selected][0].result


@dataclass
class ImportedSample:
    fq: int
    source: SourceRow
    received: date | None
    report_date: date | None
    analyses: list[Analysis] = field(default_factory=list)
    status: str = "Cadastrada"
    extra_rows: list[SourceRow] = field(default_factory=list)


@dataclass
class Plan:
    name: str
    samples: dict[int, ImportedSample] = field(default_factory=dict)
    references: list[LegacyReference] = field(default_factory=list)
    ignored_placeholders: list[int] = field(default_factory=list)
    legacy_rows: int = 0
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def analyses(self) -> list[Analysis]:
        return [analysis for sample in self.samples.values() for analysis in sample.analyses]


def rows_for(snapshot: Snapshot, plan: Plan, name: str, header: tuple) -> tuple[int, list[SourceRow]] | None:
    source = snapshot.sheets.get(name)
    if not source:
        message = f"Aba {name} ausente ou vazia."
        (plan.errors if name in ("Amostras", "Analises") else plan.warnings).append(message)
        return None
    found: list[int] = []
    for i, row in enumerate(source, 1):
        if all(h is None or key(cell(row, col)) == key(h) for col, h in enumerate(header)):
            found.append(i)
    if len(found) != 1:
        plan.errors.append(f"{name}: cabeçalho esperado não localizado de forma inequívoca.")
        return None
    # A planilha oficial tem registros B antes dos cabeçalhos das duas abas principais.
    # Só a linha do cabeçalho é excluída por posição.
    return found[0], [
        SourceRow(name, i, row) for i, row in enumerate(source, 1)
        if i != found[0] and clean(cell(row, 0))
    ]


def classify_row(plan: Plan, row: SourceRow) -> int | None:
    value = clean(row.cells[0])
    if LEGACY_CODE.fullmatch(value):
        plan.legacy_rows += 1
        return None
    number = fq_number(value)
    if number is None:
        plan.errors.append(f"{row.location}: código de amostra não reconhecido.")
    return number


def parsed_date(plan: Plan, row: SourceRow, column: int, snapshot: Snapshot) -> date | None:
    try:
        return read_date(cell(row.cells, column), snapshot.epoch)
    except ValueError:
        plan.errors.append(f"{row.location}: data inválida na coluna {chr(65 + column)}.")
        return None


def extract_analysis(plan: Plan, row: SourceRow, snapshot: Snapshot) -> AnalysisRow:
    cells = row.cells
    consolidated, direct = cell(cells, 6), cell(cells, 2)
    source_status = clean(cell(cells, 7))
    result = None
    column = None
    if row.number in snapshot.uncached_status:
        plan.errors.append(f"{row.location}: status de fórmula sem valor salvo; gere uma nova exportação.")
    try:
        result = read_result(consolidated)
        direct_result = read_result(direct)
        if result is not None:
            column = "G"
            if direct_result is not None and direct_result.identity() != result.identity():
                plan.errors.append(f"{row.location}: resultado direto (C) difere do consolidado (G).")
        elif direct_result is not None:
            result, column = direct_result, "C"
            plan.warnings.append(f"{row.location}: consolidado vazio; usado o resultado preenchido na coluna C.")
    except ValueError as error:
        plan.errors.append(f"{row.location}: {error}.")
    if key(source_status) == "concluida" and result is None:
        plan.errors.append(f"{row.location}: análise concluída sem resultado disponível para importar.")
    if key(source_status) == "pendente" and result is not None:
        plan.errors.append(f"{row.location}: análise pendente com resultado preenchido; confirme o status.")
    include_label = key(cell(cells, 8))
    include = {"sim": True, "nao": False, "": None}.get(include_label)
    if include_label not in ("sim", "nao", ""):
        plan.errors.append(f"{row.location}: campo 'Incluir no laudo?' precisa ser Sim, Não ou vazio.")
    analysis_date = parsed_date(plan, row, 5, snapshot)
    return AnalysisRow(row, result, analysis_date, source_status, include, column)


def group_analysis(plan: Plan, number: int, parameter: str, entries: list[AnalysisRow]) -> Analysis:
    if len(entries) > 1:
        line_numbers = ", ".join(str(e.source.number) for e in entries)
        plan.warnings.append(f"{fq_code(number)} / {parameter}: linhas {line_numbers} agrupadas em uma análise.")

    # Linhas idênticas não significam duas medições. Mantemos a origem de todas.
    grouped: dict[tuple, list[AnalysisRow]] = {}
    for entry in entries:
        if entry.result is not None:
            signature = (entry.result.identity(), entry.date)
            grouped.setdefault(signature, []).append(entry)
    executions = list(grouped.values())
    eligible = {
        i: [e for e in group if key(e.status) != key(CLOSED)]
        for i, group in enumerate(executions)
    }
    explicitly_reported = {i for i, group in eligible.items() if any(e.include is True for e in group)}
    candidates = explicitly_reported or {i for i, group in eligible.items() if group}
    selected = None
    if len(candidates) == 1:
        selected = next(iter(candidates))
    elif candidates and len({executions[i][0].result.identity() for i in candidates}) == 1:
        # Mesmo resultado em datas diferentes: conserva as medições e aponta a mais recente datada.
        selected = max(candidates, key=lambda i: executions[i][0].date or date.min)
    elif candidates:
        plan.errors.append(f"{fq_code(number)} / {parameter}: mais de um resultado vigente possível; revisar a aba Analises.")

    include_values = {
        e.include
        for e in (eligible[selected] if selected is not None else entries)
        if e.include is not None
    }
    if len(include_values) > 1:
        plan.errors.append(f"{fq_code(number)} / {parameter}: marcas Sim e Não incompatíveis nas linhas do mesmo resultado.")
    include = next(iter(include_values)) if len(include_values) == 1 else None
    closed = all(key(e.status) == key(CLOSED) for e in entries)
    status = "Concluída" if selected is not None else CLOSED if closed else "Pendente"
    return Analysis(parameter, entries, executions, selected, status, include)


def plan_import(snapshot: Snapshot) -> Plan:
    plan = Plan(snapshot.name)
    sample_sheet = rows_for(snapshot, plan, "Amostras", SAMPLE_HEADER)
    analysis_sheet = rows_for(snapshot, plan, "Analises", ANALYSIS_HEADER)
    if sample_sheet is None or analysis_sheet is None:
        return plan

    requested: dict[int, dict[str, list[AnalysisRow]]] = defaultdict(lambda: defaultdict(list))
    for row in analysis_sheet[1]:
        number = classify_row(plan, row)
        if number is None:
            continue
        parameter = PARAMETER_NAMES.get(key(cell(row.cells, 1)))
        if parameter is None:
            plan.errors.append(f"{row.location}: parâmetro não mapeado.")
            continue
        requested[number][parameter].append(extract_analysis(plan, row, snapshot))

    report_date_found = any(
        key(cell(row, 9)) == "data do laudo"
        for row in snapshot.sheets["Amostras"][:sample_sheet[0]]
    )
    sample_rows = defaultdict(list)
    for row in sample_sheet[1]:
        number = classify_row(plan, row)
        if number is None:
            continue
        alt = clean(cell(row.cells, 1))
        if alt and fq_number(alt) != number:
            plan.errors.append(f"{row.location}: as duas colunas de código FQ têm IDs diferentes.")
        sample_rows[number].append(row)

    for number, candidates in sample_rows.items():
        row = candidates[0]
        if len(candidates) > 1:
            identified = [r for r in candidates if any(clean(cell(r.cells, i)) for i in range(2, 8))]
            unique_fields = {tuple(clean(cell(r.cells, i)) for i in range(2, 10)) for r in candidates}
            if len(identified) == 1:
                row = identified[0]
            elif len(unique_fields) > 1:
                plan.errors.append(f"{fq_code(number)}: cadastros conflitantes em Amostras.")
            else:
                plan.warnings.append(f"{fq_code(number)}: cadastros idênticos agrupados em Amostras.")
            if len(identified) == 1:
                extras = ", ".join(str(r.number) for r in candidates if r is not row)
                plan.warnings.append(
                    f"{fq_code(number)}: cadastro da linha {row.number} usado; "
                    f"linha(s) {extras} sem identificação arquivada(s) como referência."
                )
        if not any(clean(cell(row.cells, i)) for i in range(2, 10)) and number not in requested:
            plan.ignored_placeholders.append(number)
            continue
        received = parsed_date(plan, row, 6, snapshot)
        report_date = parsed_date(plan, row, 9, snapshot) if report_date_found else None
        sample = ImportedSample(number, row, received, report_date)
        sample.extra_rows = [r for r in candidates if r is not row]
        sample.analyses = [group_analysis(plan, number, p, entries) for p, entries in requested.get(number, {}).items()]
        source_status = key(cell(row.cells, 8))
        if source_status == "enviado":
            sample.status = "Laudo enviado"
            if any(a.status == "Pendente" for a in sample.analyses):
                plan.warnings.append(f"{fq_code(number)}: laudo marcado como enviado, mas há análise pendente.")
        elif source_status == "emitido":
            sample.status = "Laudo emitido"
        elif sample.analyses:
            sample.status = (
                "Análises em andamento" if any(a.status == "Pendente" for a in sample.analyses)
                else "Resultados completos"
            )
        if source_status not in ("", "enviado", "emitido", "pendente"):
            plan.warnings.append(f"{row.location}: status histórico do laudo não mapeado; preservado na origem.")
        plan.samples[number] = sample

    for number in sorted(requested.keys() - plan.samples.keys()):
        plan.errors.append(f"{fq_code(number)}: análise sem cadastro correspondente em Amostras.")

    for sheet, (parameter, value_col, title, _) in SUPPORT_SHEETS.items():
        found = rows_for(snapshot, plan, sheet, ("Código FQ", *(None for _ in range(value_col - 1)), title))
        if found is None:
            continue
        for row in found[1]:
            number = classify_row(plan, row)
            if number is None:
                continue
            if number not in plan.samples:
                plan.warnings.append(f"{row.location}: referência {fq_code(number)} sem amostra cadastrada; não importada.")
                continue
            try:
                result = read_result(cell(row.cells, value_col))
            except ValueError:
                result = None
                plan.warnings.append(f"{row.location}: referência indisponível; conteúdo bruto preservado.")
            plan.references.append(LegacyReference(number, parameter, row, result))

    if not plan.samples:
        plan.errors.append("Nenhuma amostra FQ cadastrada foi encontrada.")
    return plan


def encode(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, default=str)


def _store_plan(db: Session, plan: Plan) -> None:
    seed_parameters(db)
    parameters = {p.key: p for p in db.scalars(select(AnalysisParameter)).all()}
    by_number = {}
    for number, sample in sorted(plan.samples.items()):
        row = sample.source.cells
        item = Sample(
            fq_number=number,
            brewery=clean(cell(row, 2)) or None,
            product=clean(cell(row, 3)) or None,
            style=clean(cell(row, 4)) or None,
            lot=clean(cell(row, 5)) or None,
            received_date=sample.received,
            due_date=deadline_for(sample.received) if sample.received else None,
            status=sample.status,
            notes=clean(cell(row, 7)) or None,
        )
        db.add(item)
        db.flush()
        by_number[number] = item.id
        db.add(AuditEvent(
            entity_type="sample", entity_id=item.id, action="spreadsheet_import",
            details_json=encode({
                **sample.source.details(), "arquivo": plan.name, "data_do_laudo": sample.report_date,
                "linhas_adicionais": [row.details() for row in sample.extra_rows],
            }),
        ))

        for analysis in sample.analyses:
            req = RequestedAnalysis(
                sample=item, parameter=parameters[analysis.parameter],
                status=analysis.status, include_in_report=analysis.include,
                # Não usa os preços vigentes para inventar tarifas históricas.
                price_snapshot=None,
            )
            db.add(req)
            db.flush()
            db.add(AuditEvent(
                entity_type="requested_analysis", entity_id=req.id, action="spreadsheet_import",
                details_json=encode({"arquivo": plan.name, "linhas": [e.source.details() for e in analysis.rows]}),
            ))
            for i, group in enumerate(analysis.executions):
                entry = group[0]
                execution = AnalysisExecution(
                    requested_analysis=req, execution_number=i + 1,
                    numeric_value=entry.result.numeric, text_value=entry.result.textual,
                    performed_at=datetime.combine(entry.date, time.min) if entry.date else None,
                    raw_inputs_json=encode({
                        "origem": "spreadsheet_import", "arquivo": plan.name,
                        "coluna_resultado": entry.value_column,
                        "linhas": [e.source.details() for e in group],
                    }),
                )
                db.add(execution)
                if i == analysis.selected:
                    req.current_result = CurrentResult(execution=execution)

    for ref in plan.references:
        db.add(AuditEvent(
            entity_type="sample", entity_id=by_number[ref.fq], action=LEGACY_REFERENCE,
            details_json=encode({
                **ref.source.details(), "arquivo": plan.name, "parametro": ref.parameter,
                "numerico": str(ref.result.numeric) if ref.result and ref.result.numeric is not None else None,
                "textual": ref.result.textual if ref.result else None,
            }),
        ))


def write_new_database(plan: Plan, output: Path) -> None:
    """Grava e verifica em uma área temporária antes de publicar um banco novo."""
    if plan.errors:
        raise ValueError("Há bloqueios na prévia; nenhum dado foi importado.")
    output = output.absolute()
    if output.suffix.lower() not in (".db", ".sqlite", ".sqlite3"):
        raise ValueError("Indique um arquivo SQLite novo com extensão .db, .sqlite ou .sqlite3.")
    if os.path.lexists(output):
        raise FileExistsError(f"O destino já existe: {output}")
    with tempfile.TemporaryDirectory(prefix=".labeer-import-", dir=output.parent) as temp:
        path = Path(temp) / "importado.db"
        path.touch(mode=0o600, exist_ok=False)
        engine = create_engine(URL.create("sqlite", database=str(path)))
        try:
            Base.metadata.create_all(engine)
            with Session(engine) as db, db.begin():
                db.execute(text("PRAGMA foreign_keys=ON"))
                _store_plan(db, plan)
            with engine.connect() as conn:
                if conn.execute(text("PRAGMA foreign_key_check")).first() is not None:
                    raise RuntimeError("Foram encontradas referências inválidas no banco temporário.")
        finally:
            engine.dispose()
        # Mesmo diretório/disco. A publicação falha se já existir um arquivo, inclusive em corrida.
        os.link(path, output)
