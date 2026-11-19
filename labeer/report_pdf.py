"""Modelo LaBeer A4. Recebe um instantâneo; não consulta nem escreve no banco."""

from __future__ import annotations

from datetime import UTC, date, datetime
from functools import lru_cache
from html import escape
from io import BytesIO
from pathlib import Path
from zoneinfo import ZoneInfo

import reportlab
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Image, LongTable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


TEMPLATE_VERSION = "1.0"
TITLE = "LAUDO DE ANÁLISE DE CERVEJA"
LAB_NAME = "LaBeer - Laboratório de análise e produção de cerveja e hidromel"
ZONE = ZoneInfo("America/Sao_Paulo")
INK = colors.HexColor("#372517")
GOLD = colors.HexColor("#DFA238")
PAPER = colors.HexColor("#F7F3EC")
LINE = colors.HexColor("#DDCDB8")
MUTED = colors.HexColor("#675B50")
LOGO = Path(__file__).with_name("assets") / "labeer.png"

# A ordem e os campos vazios seguem o formato dos laudos já usados no projeto.
RESULT_SLOTS = {
    "alcohol": ("Teor alcoólico", "% v/v"),
    "apparent_extract": ("Extrato aparente", "°P"),
    "real_extract": ("Extrato real", "°P"),
    "original_extract": ("Extrato original", "°P"),
    "color": ("Cor", "ASBC"),
    "ibu": ("Amargor", "IBU"),
    "calories": ("Calorias", "kcal/100 mL"),
    "density": ("Densidade", "g/cm³"),
    "ph": ("pH", None),
    "carbohydrates": ("Carboidratos", "g/100 mL"),
    "shelf_life": ("Shelf Life", "dias"),
    "gluten": ("Glúten", "ppm"),
}


def local_timestamp(value: datetime) -> datetime:
    return (value.replace(tzinfo=UTC) if value.tzinfo is None else value).astimezone(ZONE)


@lru_cache(maxsize=1)
def register_fonts() -> None:
    # Fontes redistribuídas com o próprio ReportLab; os glifos ≤ e ≥ saem como texto.
    base = Path(reportlab.__file__).parent / "fonts"
    pdfmetrics.registerFont(TTFont("LaBeerBody", str(base / "Vera.ttf")))
    pdfmetrics.registerFont(TTFont("LaBeerBold", str(base / "VeraBd.ttf")))


def report_filename(code: str, number: int, *, preview: bool = False) -> str:
    prefix = "Previa" if preview else "Laudo"
    return f"{prefix}_{code}_v{number:02d}.pdf"


def _paragraph(value: str | None, style: ParagraphStyle) -> Paragraph:
    return Paragraph(escape(value or "---").replace("\n", "<br/>"), style)


def render_report_pdf(
    content: dict,
    *,
    number: int,
    issued_at: datetime | None = None,
    replaces_number: int | None = None,
) -> bytes:
    register_fonts()
    sample = content["sample"]
    code = sample["code"]
    preview = issued_at is None
    output = BytesIO()
    document = SimpleDocTemplate(
        output, pagesize=A4, leftMargin=48, rightMargin=48, topMargin=38, bottomMargin=54,
        title=f"{'Prévia' if preview else 'Laudo'} {code} v{number:02d}", author="LaBeer",
    )
    width = document.width - 12  # Margens internas da moldura do ReportLab.
    heading = ParagraphStyle("lab", fontName="LaBeerBold", fontSize=13, leading=16, textColor=colors.white,
                             alignment=TA_CENTER)
    title = ParagraphStyle("title", fontName="LaBeerBold", fontSize=13, leading=16, textColor=INK, alignment=TA_CENTER)
    section = ParagraphStyle("section", fontName="LaBeerBold", fontSize=11, leading=14, textColor=colors.white,
                             alignment=TA_CENTER)
    label = ParagraphStyle("label", fontName="LaBeerBold", fontSize=9.3, leading=13, textColor=INK)
    body = ParagraphStyle("body", fontName="LaBeerBody", fontSize=9.5, leading=13.5, textColor=INK)
    metadata = ParagraphStyle("small", fontName="LaBeerBody", fontSize=8.5, leading=11.5, textColor=MUTED)
    result = ParagraphStyle("result", parent=body, alignment=TA_CENTER)

    def bar(value: str, style: ParagraphStyle, fill: colors.Color) -> Table:
        return Table([[_paragraph(value, style)]], colWidths=[width], style=[
            ("BACKGROUND", (0, 0), (-1, -1), fill),
            ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ])

    logo = Image(str(LOGO), width=60, height=60)
    lab_header = Table(
        [[_paragraph(LAB_NAME, heading)], [_paragraph("Registro SIEX: 302400", metadata)]],
        colWidths=[width - 76],
        style=[("BACKGROUND", (0, 0), (0, 0), INK), ("TOPPADDING", (0, 0), (0, 0), 8),
               ("BOTTOMPADDING", (0, 0), (0, 0), 8), ("TOPPADDING", (0, 1), (0, 1), 6)],
    )
    header = Table([[logo, lab_header]], colWidths=[76, width - 76], style=[
        ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 0),
    ])
    issue_label = f"Versão {number:02d} no aplicativo"
    if issued_at is not None:
        issue_label += "   |   Emissão: " + local_timestamp(issued_at).strftime("%d/%m/%Y")
    if replaces_number is not None:
        issue_label += f"   |   Substitui a versão {replaces_number:02d}"

    parts = [header, Spacer(1, 8), bar(TITLE, title, GOLD), Spacer(1, 7)]
    if preview:
        parts.extend([bar("PRÉVIA - NÃO EMITIDO", section, colors.HexColor("#7C3B26")), Spacer(1, 7)])
    parts.extend([_paragraph(issue_label, metadata), Spacer(1, 10)])

    fields = [
        ("Código FQ", code), ("Cervejaria", sample["brewery"]),
        ("Nome da cerveja", sample["product"]), ("Lote", sample["lot"]),
    ]
    if sample.get("style") and sample["style"] != "---":
        fields.append(("Estilo", sample["style"]))
    if sample.get("received_on"):
        fields.append(("Recebimento", date.fromisoformat(sample["received_on"]).strftime("%d/%m/%Y")))
    common = [
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 12), ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("BACKGROUND", (1, 0), (1, -1), PAPER),
        ("BOX", (0, 0), (-1, -1), 0.5, LINE), ("LINEBEFORE", (1, 0), (1, -1), 0.45, LINE),
    ]
    parts.extend([
        Table([[_paragraph(k, label), _paragraph(v, body)] for k, v in fields],
              colWidths=[148, width - 148], style=common),
        Spacer(1, 14), bar("RESULTADOS ANALÍTICOS", section, INK), Spacer(1, 8),
    ])

    by_key = {row["key"]: row for row in content["results"]}
    slots = {**RESULT_SLOTS}
    for key, row in by_key.items():
        if key not in slots:
            slots[key] = (row["name"], row["unit"])
    rows = [[_paragraph("Parâmetro", label), _paragraph("Resultado", result)]]
    for key, (name, default_unit) in slots.items():
        used = by_key.get(key)
        unit = used["unit"] if used else default_unit
        text = used["display"] if used else "---"
        rows.append([_paragraph(f"{name} ({unit})" if unit else name, label), _paragraph(text, result)])
    parts.append(LongTable(
        rows, colWidths=[width * 0.68, width * 0.32], repeatRows=1, style=TableStyle(
            common + [("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EEE3D5")),
                      ("LINEBELOW", (0, 0), (-1, 0), 0.6, LINE),
                      ("LINEBELOW", (0, 1), (-1, -2), 0.24, LINE)],
        ),
    ))
    if content.get("note"):
        parts.extend([Spacer(1, 12), _paragraph("Observação", label), Spacer(1, 3), _paragraph(content["note"], body)])

    def footer(canvas, doc):
        height = 36
        left, right = doc.leftMargin + 6, A4[0] - doc.rightMargin - 6
        canvas.setStrokeColor(LINE)
        canvas.line(left, height + 11, right, height + 11)
        canvas.setFont("LaBeerBody", 8)
        canvas.setFillColor(MUTED)
        canvas.drawString(left, height, f"LaBeer   |   {code}   |   v{number:02d}" + ("   |   PRÉVIA" if preview else ""))
        canvas.drawRightString(right, height, f"Página {doc.page}")

    document.build(parts, onFirstPage=footer, onLaterPages=footer)
    return output.getvalue()
