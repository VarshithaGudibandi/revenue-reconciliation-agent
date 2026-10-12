"""Facts about the columns of a source file, computed locally.

A profile is what a mapper may look at. Parse statistics are computed on a bounded sample;
the distinct values are kept for cross-file checks and are not part of what is described
to anything outside the process (see ``describe``).
"""

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from recon.domain.money import CURRENCY_EXPONENTS, MoneyError, parse_amount
from recon.ingestion.normalize import MappingError

DATE_FORMATS = (
    "%Y-%m-%d",
    "%d/%m/%Y",
    "%m/%d/%Y",
    "%d.%m.%Y",
    "%m.%d.%Y",
    "%d-%m-%Y",
    "%m-%d-%Y",
    "%d-%b-%Y",
    "%d %b %Y",
    "%Y/%m/%d",
    "%Y%m%d",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%SZ",
)
SAMPLE_LIMIT = 2000
MIN_YEAR, MAX_YEAR = 1990, 2100
_RUN = re.compile(r"(.)\1+")


@dataclass(frozen=True)
class ColumnProfile:
    header: str
    position: int
    rows: int
    non_empty: int
    distinct: int
    date_rates: dict[str, float]
    amount_rates: dict[str, float]
    fraction_share: float
    currency_share: float
    shapes: tuple[tuple[str, int], ...]
    values: tuple[str, ...] = field(repr=False, compare=False)

    @property
    def unique_share(self) -> float:
        return self.distinct / self.non_empty if self.non_empty else 0.0

    @property
    def empty_share(self) -> float:
        return 1 - self.non_empty / self.rows if self.rows else 1.0

    def date_formats(self, threshold: float = 0.98) -> tuple[str, ...]:
        """Every format under which nearly all values read as plausible dates."""
        return tuple(f for f, rate in self.date_rates.items() if rate >= threshold)

    def date_like(self) -> bool:
        return bool(self.date_formats())

    def amount_separators(self, threshold: float = 0.98) -> tuple[str, ...]:
        return tuple(s for s, rate in self.amount_rates.items() if rate >= threshold)

    def amount_like(self) -> bool:
        """Parses as a number with a fractional part. Whole-number columns, such as numeric
        ids, are deliberately not treated as amounts."""
        return bool(self.amount_separators()) and self.fraction_share >= 0.3

    def currency_like(self) -> bool:
        return self.currency_share >= 0.95


@dataclass(frozen=True)
class FileProfile:
    name: str
    rows: int
    columns: tuple[ColumnProfile, ...]
    cells: dict[str, tuple[str, ...]] = field(repr=False, compare=False)

    def column(self, header: str) -> ColumnProfile:
        return next(c for c in self.columns if c.header == header)

    def headers(self) -> tuple[str, ...]:
        return tuple(c.header for c in self.columns)

    def describe(self, *, samples: bool = False) -> dict[str, Any]:
        """The profile as plain data. By default no cell value appears: shapes replace values,
        so ``INV-000123`` is ``A+-9+``. This is what may be sent to a language model.

        With ``samples``, each column also gets up to three examples. Short all-capital codes
        in a column with few distinct values (``EUR``, ``OPEN``) are shown as they are; every
        other example is masked by ``mask_value``.
        """
        columns = []
        for c in self.columns:
            entry: dict[str, Any] = {
                "header": c.header,
                "position": c.position,
                "empty_share": round(c.empty_share, 3),
                "unique_share": round(c.unique_share, 3),
                "shapes": [{"shape": s, "count": n} for s, n in c.shapes],
                "date_formats": list(c.date_formats()),
                "amount_decimal_separators": list(c.amount_separators()),
                "share_with_fraction": round(c.fraction_share, 3),
                "share_currency_codes": round(c.currency_share, 3),
            }
            if samples:
                distinct = list(dict.fromkeys(c.values))
                if _is_label_column(c):
                    entry["examples"] = {"kind": "codes", "values": sorted(distinct)}
                else:
                    entry["examples"] = {
                        "kind": "masked",
                        "values": [mask_value(v) for v in distinct[:3]],
                    }
            columns.append(entry)
        return {"file": self.name, "rows": self.rows, "columns": columns}


def shape_of(value: str) -> str:
    """Digits become 9, letters A or a, and runs collapse: ``INV-000123`` -> ``A+-9+``."""
    masked = "".join(
        "9" if ch.isdigit() else ("A" if ch.isupper() else "a") if ch.isalpha() else ch
        for ch in value
    )
    return _RUN.sub(lambda m: m.group(1) + "+", masked)


_TOKENS = re.compile(r"(\d+)|([^\W\d_]+)|(.)", re.DOTALL)
LABEL_LIMIT = 8


def mask_value(value: str) -> str:
    """Hide most of a value but keep its form: digits become ``*`` after the first of each run,
    and letters after the first of each run longer than three. ``INV-000123`` becomes
    ``INV-0*****``, ``ACME-CORP`` becomes ``A***-C***`` and ``1500.00`` becomes ``1***.0*``.
    Punctuation and separators stay, because they carry the format.
    """
    parts: list[str] = []
    for digits, letters, other in _TOKENS.findall(value):
        if digits:
            parts.append(digits[0] + "*" * (len(digits) - 1))
        elif letters:
            parts.append(letters if len(letters) <= 3 else letters[0] + "*" * (len(letters) - 1))
        else:
            parts.append(other)
    return "".join(parts)


def _is_label_column(column: "ColumnProfile") -> bool:
    """Short all-capital codes with few distinct values, such as a currency or a status."""
    distinct = set(column.values)
    return 0 < len(distinct) <= LABEL_LIMIT and all(
        v.isascii() and v.isalpha() and v.isupper() and len(v) <= 6 for v in distinct
    )


def _plausible(moment: datetime) -> bool:
    return MIN_YEAR <= moment.year <= MAX_YEAR


def _date_rate(values: list[str], date_format: str) -> float:
    ok = 0
    for value in values:
        try:
            if _plausible(datetime.strptime(value, date_format)):  # noqa: DTZ007
                ok += 1
        except ValueError:
            continue
    return ok / len(values) if values else 0.0


def _amount_rate(values: list[str], separator: str) -> float:
    ok = 0
    for value in values:
        try:
            parse_amount(value, "USD", decimal_separator=separator)
            ok += 1
        except (MoneyError, ValueError):
            continue
    return ok / len(values) if values else 0.0


def _profile_column(header: str, position: int, cells: tuple[str, ...]) -> ColumnProfile:
    values = [c for c in cells if c]
    sample = values[:SAMPLE_LIMIT]
    shapes: dict[str, int] = {}
    for value in sample:
        shape = shape_of(value)
        shapes[shape] = shapes.get(shape, 0) + 1
    fraction = sum(1 for v in sample if re.search(r"[.,]\d{1,2}$", v))
    return ColumnProfile(
        header=header,
        position=position,
        rows=len(cells),
        non_empty=len(values),
        distinct=len(set(values)),
        date_rates={f: _date_rate(sample, f) for f in DATE_FORMATS},
        amount_rates={s: _amount_rate(sample, s) for s in (".", ",")},
        fraction_share=fraction / len(sample) if sample else 0.0,
        currency_share=(
            sum(1 for v in sample if v.upper() in CURRENCY_EXPONENTS) / len(sample)
            if sample
            else 0.0
        ),
        shapes=tuple(sorted(shapes.items(), key=lambda item: (-item[1], item[0]))[:5]),
        values=tuple(values),
    )


def profile_csv_bytes(name: str, data: bytes) -> FileProfile:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise MappingError(f"{name} is not valid UTF-8") from error
    reader = csv.reader(io.StringIO(text, newline=""))
    try:
        headers = [h.strip() for h in next(reader)]
    except StopIteration:
        raise MappingError(f"{name} is empty") from None
    if not any(headers) or len(set(headers)) != len(headers):
        raise MappingError(f"{name} needs a header row with distinct, non-empty names")
    rows = [[(cell or "").strip() for cell in row] for row in reader if any(row)]
    columns = tuple(
        _profile_column(
            header, index, tuple(row[index] if index < len(row) else "" for row in rows)
        )
        for index, header in enumerate(headers)
    )
    return FileProfile(
        name=name,
        rows=len(rows),
        columns=columns,
        cells={
            c.header: tuple(r[c.position] if c.position < len(r) else "" for r in rows)
            for c in columns
        },
    )
