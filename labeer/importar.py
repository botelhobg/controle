"""Prévia e gravação local: python -m labeer.importar --help"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from zipfile import BadZipFile

from .importer import Plan, clean, cell, fq_number, plan_import, read_xlsx, write_new_database
from .models import fq_code
from .services import PARAMETER_SEED


PARAMETERS = {code: (name, unit) for code, name, unit, *_ in PARAMETER_SEED}


def format_check_result(parameter, result) -> str:
    if result is None:
        return "sem resultado"
    value = result.as_text()
    unit = PARAMETERS[parameter][1]
    if unit and (result.numeric is not None or (value[0] in "<>≤≥" and not any(c.isalpha() for c in value))):
        return f"{value} {unit}"
    return value


def print_preview(plan: Plan, selected: list[int]) -> None:
    analyses = plan.analyses
    print(f"Origem: {plan.name}")
    print(f"Amostras FQ: {len(plan.samples)} | Análises distintas: {len(analyses)}")
    print(f"Resultados vigentes: {sum(a.current is not None for a in analyses)}")
    print(f"Análises pendentes: {sum(a.status == 'Pendente' for a in analyses)}")
    print(f"Fechadas sem resultado: {sum(a.status == 'Encerrada sem resultado' for a in analyses)}")
    print(f"Linhas de referências preservadas: {len(plan.references)}")
    print(f"Linhas de códigos B descartadas: {plan.legacy_rows}")
    if plan.ignored_placeholders:
        print("Códigos sem cadastro ignorados: " + ", ".join(map(fq_code, sorted(plan.ignored_placeholders))))
    missing_date = sum(s.received is None for s in plan.samples.values())
    if missing_date:
        print(f"Recebimento e prazo sem data: {missing_date} amostra(s).")
    if plan.samples:
        print(f"Próximo código: {fq_code(max(plan.samples) + 1)}")
    if plan.warnings:
        print("\nAvisos:")
        for warning in plan.warnings:
            print(f"  - {warning}")
    if plan.errors:
        print("\nBloqueios:")
        for error in plan.errors:
            print(f"  - {error}")
    for number in selected:
        sample = plan.samples.get(number)
        if sample is None:
            print(f"\n{fq_code(number)}: não encontrada entre as amostras cadastradas.")
            continue
        print(f"\n{fq_code(number)} — {sample.status}")
        for analysis in sample.analyses:
            p = analysis.parameter
            print(f"  {PARAMETERS[p][0]}: {format_check_result(p, analysis.current)} · {analysis.status}")
            for i, group in enumerate(analysis.executions):
                where = ", ".join(entry.source.location for entry in group)
                rule = "vigente" if i == analysis.selected else "não vigente"
                print(f"    {where}: {format_check_result(p, group[0].result)} ({rule})")
        for ref in plan.references:
            if ref.fq != number:
                continue
            print(
                f"  {PARAMETERS[ref.parameter][0]} — {ref.source.location}: "
                f"{format_check_result(ref.parameter, ref.result)} (referência)"
            )


def parse_code(value: str) -> int:
    number = fq_number(value)
    if number is None:
        raise argparse.ArgumentTypeError("Informe um código FQ, por exemplo FQ69.")
    return number


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Importa uma cópia .xlsx da planilha oficial do LaBeer.")
    parser.add_argument("planilha", type=Path, help="Caminho do arquivo .xlsx exportado do Google Sheets.")
    parser.add_argument("--conferir", type=parse_code, nargs="+", default=[], metavar="FQ",
                        help="Mostra os resultados e origens dos códigos indicados.")
    parser.add_argument("--aplicar", type=Path, metavar="BANCO_NOVO.db",
                        help="Grava em um novo SQLite; sem esta opção, só mostra a prévia.")
    args = parser.parse_args(argv)

    try:
        plan = plan_import(read_xlsx(args.planilha))
        print_preview(plan, args.conferir)
        if plan.errors:
            print("\nImportação bloqueada. Nenhum banco alterado.", file=sys.stderr)
            return 2
        if args.aplicar is None:
            print("\nSó prévia. Nenhum banco alterado.")
        else:
            write_new_database(plan, args.aplicar)
            print(f"\nImportação concluída no banco novo: {args.aplicar}")
    except (OSError, ValueError, BadZipFile) as error:
        print(f"Não foi possível concluir: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
