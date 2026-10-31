"""Streaming CSV loader. No sorting, coercion via float, or discarded rows."""

import csv
import re
from dataclasses import dataclass
from decimal import Decimal
from itertools import zip_longest
from pathlib import Path
from typing import Callable, Generic, Iterator, TypeVar

ASK_MISSING = 9_999_999_999
BID_MISSING = -9_999_999_999
INTEGER = re.compile(r"[+-]?[0-9]+\Z")
TIME = re.compile(r"[0-9]+(?:\.[0-9]+)?\Z")
T = TypeVar("T")


def parse_integer(token: str) -> int:
    if not INTEGER.fullmatch(token):
        raise ValueError("expected decimal integer")
    return int(token)


def parse_seconds(token: str) -> Decimal:
    """Keep even vendor timestamps whose precision exceeds the documented ns."""
    if not TIME.fullmatch(token):
        raise ValueError("expected nonnegative decimal seconds")
    return Decimal(token)


def exact_ns(seconds: Decimal) -> int | None:
    numerator, denominator = seconds.as_integer_ratio()
    ns, remainder = divmod(numerator * 1_000_000_000, denominator)
    return None if remainder else ns


@dataclass(frozen=True)
class Message:
    time_seconds: Decimal
    event_type: int
    order_id: int
    size: int
    price: int
    direction: int

    @property
    def time_ns(self) -> int | None:
        """None means the source timestamp is not exactly representable in ns."""
        return exact_ns(self.time_seconds)


@dataclass(frozen=True)
class Level:
    ask_price: int
    ask_size: int
    bid_price: int
    bid_size: int


@dataclass(frozen=True)
class Book:
    levels: tuple[Level, ...]


@dataclass(frozen=True)
class CSVRow(Generic[T]):
    line: int
    raw: str
    value: T | None
    errors: tuple[str, ...]


@dataclass(frozen=True)
class PairedRow:
    line: int
    message: CSVRow[Message] | None
    orderbook: CSVRow[Book] | None


def _read_rows(
    path: Path, columns: int, source: str, converters: list[Callable[[str], int | Decimal]],
    construct: Callable[[list], T],
) -> Iterator[CSVRow[T]]:
    # LOBSTER uses one physical line per record, without embedded newlines.
    # Parse lines separately so a malformed quote cannot consume subsequent rows.
    with path.open(encoding="utf-8", newline="") as handle:
        for line, raw in enumerate(handle, 1):
            try:
                fields = next(csv.reader([raw], strict=True))
            except csv.Error:
                yield CSVRow(line, raw, None, (f"{source}.csv_syntax",))
                continue
            if len(fields) != columns:
                yield CSVRow(line, raw, None, (f"{source}.column_count",))
                continue
            values = []
            errors = []
            for index, (token, convert) in enumerate(zip(fields, converters)):
                try:
                    values.append(convert(token))
                except ValueError:
                    reason = "time_format" if source == "message" and index == 0 else "integer_format"
                    errors.append(f"{source}.{reason}")
            yield CSVRow(line, raw, None if errors else construct(values), tuple(sorted(set(errors))))


def iter_messages(path: str | Path) -> Iterator[CSVRow[Message]]:
    return _read_rows(Path(path), 6, "message", [parse_seconds] + [parse_integer] * 5,
                      lambda fields: Message(*fields))


def iter_orderbooks(path: str | Path, levels: int = 5) -> Iterator[CSVRow[Book]]:
    if levels < 1:
        raise ValueError("levels must be positive")
    return _read_rows(
        Path(path), 4 * levels, "orderbook", [parse_integer] * (4 * levels),
        lambda fields: Book(tuple(Level(*fields[i:i + 4]) for i in range(0, len(fields), 4))),
    )


def iter_pairs(
    message: str | Path, orderbook: str | Path, levels: int = 5,
) -> Iterator[PairedRow]:
    """Yield every source position, including parse failures and unmatched tails."""
    for line, (msg, book) in enumerate(
        zip_longest(iter_messages(message), iter_orderbooks(orderbook, levels)), 1
    ):
        yield PairedRow(line, msg, book)

