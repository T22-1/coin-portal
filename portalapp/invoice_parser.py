from __future__ import annotations

import csv
import json
import os
import re
import urllib.error
import urllib.request
from decimal import Decimal, InvalidOperation
from io import BytesIO, StringIO
from pathlib import Path

from pypdf import PdfReader


DENOMINATION_PATTERNS = [
    ("50c", r"\b(50c|half dollar|half)\b"),
    ("25c", r"\b(25c|quarter)\b"),
    ("10c", r"\b(10c|dime)\b"),
    ("5c", r"\b(5c|nickel)\b"),
    ("3c", r"\b(3c|three cent)\b"),
    ("1c", r"\b(1c|cent|penny)\b"),
    ("$1", r"\b(\$1|1\$|dollar)\b"),
    ("$2.50", r"\b(\$2\.50|2\.5|quarter eagle)\b"),
    ("$5", r"\b(\$5|half eagle)\b"),
    ("$10", r"\b(\$10|eagle)\b"),
    ("$20", r"\b(\$20|double eagle)\b"),
]

HOLDER_PATTERN = re.compile(r"\b(PCGS|NGC|CACG|CAC|ANACS|ICG|RAW)\b", re.IGNORECASE)
GRADE_PATTERN = re.compile(
    r"\b((?:PR|PF|MS|AU|XF|EF|VF|F|VG|G|AG|FR|PO)\s?\d{1,2}(?:[+\-])?(?:\s?(?:BN|RB|RD|CAM|DCAM|UCAM|PL|DMPL|FH|FB|FS|FBL))?|RAW|DETAILS)\b",
    re.IGNORECASE,
)
CERT_PATTERN = re.compile(r"\b(?:cert(?:ificate)?\s*#?:?\s*)?(\d{6,12})\b", re.IGNORECASE)
DATE_PATTERN = re.compile(r"\b(1[5-9]\d{2}|20\d{2})(?:[-\s]?([A-Z]{1,2}))?\b", re.IGNORECASE)
MONEY_PATTERN = re.compile(r"(?:\$|USD\s*)?([0-9][0-9,]*(?:\.\d{2})?)")


def parse_invoice_file(uploaded_file) -> tuple[str, list[dict], str]:
    filename = Path(uploaded_file.name).name
    data = uploaded_file.read()
    uploaded_file.seek(0)

    text = _extract_text(filename, data)
    rows = _parse_with_ai(text) if os.getenv("OPENAI_API_KEY") else []
    notes = "Parsed with AI." if rows else "Parsed with local invoice reader. Review rows before importing."
    if not rows:
        rows = _parse_locally(filename, text)
    if not rows and text.strip():
        rows = [_line_from_text(line) for line in text.splitlines() if _looks_like_coin_line(line)]
    return text, rows, notes


def _extract_text(filename: str, data: bytes) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix == ".pdf":
        reader = PdfReader(BytesIO(data))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="ignore")


def _parse_with_ai(text: str) -> list[dict]:
    cleaned = text.strip()
    if not cleaned:
        return []

    prompt = (
        "Extract coin inventory rows from this invoice text. Return only JSON with a top-level "
        "'items' array. Each item can include raw_description, date_mm, denomination, series, "
        "variety, holder, grade_text, cert_number, ask_price, cost_basis, source, confidence, "
        "and needs_review. Use empty strings for unknown text fields. confidence is 0-100. "
        "Do not invent values.\n\n"
        f"{cleaned[:24000]}"
    )
    payload = {
        "model": os.getenv("OPENAI_INVOICE_MODEL", "gpt-4.1-mini"),
        "input": prompt,
    }
    request = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=35) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError):
        return []

    output_text = body.get("output_text") or ""
    if not output_text:
        for item in body.get("output", []):
            for content in item.get("content", []):
                if content.get("type") in {"output_text", "text"}:
                    output_text += content.get("text", "")
    try:
        parsed = json.loads(_json_object_from_text(output_text))
    except json.JSONDecodeError:
        return []
    return [_normalize_row(row) for row in parsed.get("items", []) if isinstance(row, dict)]


def _json_object_from_text(text: str) -> str:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return "{}"
    return text[start : end + 1]


def _parse_locally(filename: str, text: str) -> list[dict]:
    suffix = Path(filename).suffix.lower()
    if suffix in {".csv", ".tsv"}:
        delimiter = "\t" if suffix == ".tsv" else ","
        return _parse_table(text, delimiter)
    return [_line_from_text(line) for line in text.splitlines() if _looks_like_coin_line(line)]


def _parse_table(text: str, delimiter: str) -> list[dict]:
    rows = []
    reader = csv.DictReader(StringIO(text), delimiter=delimiter)
    if not reader.fieldnames:
        return rows

    for source_row in reader:
        lowered = {str(key).strip().lower(): (value or "").strip() for key, value in source_row.items()}
        description = _first_value(lowered, "description", "coin", "item", "name", "title")
        combined = " ".join(value for value in lowered.values() if value)
        row = _line_from_text(description or combined)
        row.update(
            {
                "raw_description": description or combined,
                "date_mm": _first_value(lowered, "date", "date/mm", "date_mm", "year") or row["date_mm"],
                "denomination": _first_value(lowered, "denom", "denomination") or row["denomination"],
                "series": _first_value(lowered, "series", "type") or row["series"],
                "variety": _first_value(lowered, "variety", "vam", "overton") or row["variety"],
                "holder": _first_value(lowered, "holder", "service", "grading service") or row["holder"],
                "grade_text": _first_value(lowered, "grade", "grade_text") or row["grade_text"],
                "cert_number": _first_value(lowered, "cert", "cert #", "cert_number", "certification") or row["cert_number"],
                "ask_price": _clean_decimal(_first_value(lowered, "ask", "ask price", "retail", "price")),
                "cost_basis": _clean_decimal(_first_value(lowered, "cost", "cost basis", "wholesale", "paid")),
            }
        )
        row["needs_review"] = _needs_review(row)
        row["confidence"] = 80 if not row["needs_review"] else 45
        rows.append(_normalize_row(row))
    return rows


def _first_value(row: dict[str, str], *names: str) -> str:
    for name in names:
        if row.get(name):
            return row[name]
    return ""


def _looks_like_coin_line(line: str) -> bool:
    stripped = line.strip()
    if len(stripped) < 8:
        return False
    return bool(DATE_PATTERN.search(stripped) or HOLDER_PATTERN.search(stripped) or GRADE_PATTERN.search(stripped))


def _line_from_text(line: str) -> dict:
    text = " ".join(line.split())
    date_match = DATE_PATTERN.search(text)
    holder_match = HOLDER_PATTERN.search(text)
    grade_match = GRADE_PATTERN.search(text)
    cert_match = CERT_PATTERN.search(text)
    denomination = ""
    for value, pattern in DENOMINATION_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            denomination = value
            break

    date_mm = ""
    if date_match:
        date_mm = date_match.group(1)
        if date_match.group(2):
            date_mm = f"{date_mm}-{date_match.group(2).upper()}"

    series = text
    for token in (date_mm, denomination, holder_match.group(0) if holder_match else "", grade_match.group(0) if grade_match else ""):
        if token:
            series = series.replace(token, " ")
    series = MONEY_PATTERN.sub(" ", series)
    series = CERT_PATTERN.sub(" ", series)
    series = " ".join(series.split(" -:,"))
    if len(series) > 120:
        series = series[:120]

    money_values = MONEY_PATTERN.findall(text)
    cost_basis = _clean_decimal(money_values[-1]) if money_values else None
    row = {
        "raw_description": text,
        "date_mm": date_mm,
        "denomination": denomination,
        "series": series,
        "variety": "",
        "holder": holder_match.group(0).upper() if holder_match else "",
        "grade_text": grade_match.group(0).replace(" ", "").upper() if grade_match else "",
        "cert_number": cert_match.group(1) if cert_match else "",
        "ask_price": None,
        "cost_basis": cost_basis,
        "source": "",
    }
    row["needs_review"] = _needs_review(row)
    row["confidence"] = 70 if not row["needs_review"] else 35
    return _normalize_row(row)


def _clean_decimal(value):
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value).replace("$", "").replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None


def _needs_review(row: dict) -> bool:
    return not bool(row.get("date_mm") and row.get("denomination") and row.get("series"))


def _normalize_row(row: dict) -> dict:
    return {
        "raw_description": str(row.get("raw_description") or "")[:1000],
        "date_mm": str(row.get("date_mm") or "")[:20],
        "denomination": str(row.get("denomination") or "")[:60],
        "series": str(row.get("series") or "")[:120],
        "variety": str(row.get("variety") or "")[:120],
        "holder": str(row.get("holder") or "")[:20].upper(),
        "grade_text": str(row.get("grade_text") or "")[:40],
        "cert_number": str(row.get("cert_number") or "")[:40],
        "ask_price": _clean_decimal(row.get("ask_price")),
        "cost_basis": _clean_decimal(row.get("cost_basis")),
        "source": str(row.get("source") or "")[:120],
        "confidence": max(0, min(100, int(row.get("confidence") or 0))),
        "needs_review": bool(row.get("needs_review", True)),
    }
