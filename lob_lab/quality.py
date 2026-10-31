"""Row-counted diagnostics and exact top-of-book statistics."""

from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path

from .loader import ASK_MISSING, BID_MISSING, Book, Message, exact_ns, iter_pairs

EVENT_TYPES = {
    1: "submission", 2: "partial_cancellation", 3: "deletion",
    4: "visible_execution", 5: "hidden_execution", 6: "cross_trade", 7: "trading_halt",
}


def _message_reasons(message: Message) -> set[str]:
    reasons = set()
    if not 0 <= message.time_seconds < 86_400:
        reasons.add("message.time_out_of_day")
    if message.time_seconds.as_tuple().exponent < -9:
        reasons.add("message.time_precision_gt_ns")
    if message.event_type not in EVENT_TYPES:
        reasons.add("message.unknown_event_type")
    if message.order_id < 0:
        reasons.add("message.negative_order_id")
    if message.size < 0:
        reasons.add("message.negative_size")
    if message.event_type == 7:
        if (message.price not in (-1, 0, 1) or message.direction != -1
                or message.order_id != 0 or message.size != 0):
            reasons.add("message.invalid_halt_marker")
    else:
        if message.size == 0:
            reasons.add("message.zero_size")
        if not 0 < message.price < ASK_MISSING:
            reasons.add("message.invalid_price")
        # Type 6 has no resting-order side; do not infer one from its direction.
        if message.event_type != 6 and message.direction not in (-1, 1):
            reasons.add("message.invalid_direction")
    return reasons


def _book_reasons(book: Book, observations: Counter) -> set[str]:
    reasons = set()
    empty_sides = 0
    for side, sentinel in (("ask", ASK_MISSING), ("bid", BID_MISSING)):
        previous = None
        missing_seen = False
        occupied = 0
        for level in book.levels:
            price = getattr(level, f"{side}_price")
            size = getattr(level, f"{side}_size")
            if size < 0:
                reasons.add(f"orderbook.{side}.negative_size")
            if size == 0:
                observations[f"{side}_zero_size_cells"] += 1
            if price == sentinel:
                missing_seen = True
                if size != 0:
                    reasons.add(f"orderbook.{side}.sentinel_nonzero_size")
                else:
                    observations[f"{side}_missing_levels"] += 1
                continue
            if not 0 < price < ASK_MISSING:
                reasons.add(f"orderbook.{side}.invalid_price")
            if size == 0:
                reasons.add(f"orderbook.{side}.zero_size_occupied")
            if missing_seen:
                reasons.add(f"orderbook.{side}.occupied_after_missing")
            if previous is not None and (
                (side == "ask" and price <= previous) or (side == "bid" and price >= previous)
            ):
                reasons.add(f"orderbook.{side}.level_order")
            previous = price
            if 0 < price < ASK_MISSING and size > 0:
                occupied += 1
        if occupied == 0:
            observations[f"{side}_empty_rows"] += 1
            empty_sides += 1
    if empty_sides:
        reasons.add("orderbook.empty_side")
        observations["one_or_both_sides_empty_rows"] += 1
    if empty_sides == 2:
        observations["both_sides_empty_rows"] += 1
    return reasons


def _top(book: Book) -> tuple[int, int] | None:
    best = book.levels[0]
    if (best.ask_size > 0 and best.bid_size > 0
            and 0 < best.ask_price < ASK_MISSING and 0 < best.bid_price < ASK_MISSING):
        return best.ask_price, best.bid_price
    return None


def _spread_summary(histogram: Counter) -> dict:
    count = sum(histogram.values())
    if not count:
        return {"count": 0, "histogram": {}, "min": None, "max": None,
                "mean": None, "p50": None, "p95": None, "p99": None}
    ordered = sorted(histogram)

    def quantile(percent: int) -> int:
        rank = (count * percent + 99) // 100  # nearest rank
        cumulative = 0
        for value in ordered:
            cumulative += histogram[value]
            if cumulative >= rank:
                return value
        raise AssertionError("unreachable")

    return {
        "count": count,
        "histogram": {str(value): histogram[value] for value in ordered},
        "min": ordered[0], "max": ordered[-1],
        "mean": str(Decimal(sum(k * v for k, v in histogram.items())) / Decimal(count)),
        "p50": quantile(50), "p95": quantile(95), "p99": quantile(99),
    }


def quality_report(message: str | Path, orderbook: str | Path, levels: int = 5) -> dict:
    counts = Counter(message_rows=0, orderbook_rows=0, paired_rows=0,
                     parsed_message_rows=0, parsed_orderbook_rows=0, suspicious_rows=0)
    observations = Counter({name: 0 for name in (
        "time_ties", "message_zero_size_rows", "halt_marker_rows",
        "ask_zero_size_cells", "bid_zero_size_cells", "ask_missing_levels", "bid_missing_levels",
        "ask_empty_rows", "bid_empty_rows", "one_or_both_sides_empty_rows", "both_sides_empty_rows",
        "locked_rows", "crossed_rows",
    )})
    events: Counter[int] = Counter({kind: 0 for kind in EVENT_TYPES})
    spreads: Counter[int] = Counter()
    issues: Counter[str] = Counter()
    examples: dict[str, list[int]] = defaultdict(list)
    first_time = last_time = min_time = max_time = previous_time = None
    previous_mid = None
    previous_book = None
    mid_changes = mid_comparisons = 0
    for row in iter_pairs(message, orderbook, levels):
        reasons: set[str] = set()
        for source, value in (("message", row.message), ("orderbook", row.orderbook)):
            if value is None:
                reasons.add(f"pair.missing_{source}")
            else:
                counts[f"{source}_rows"] += 1
                reasons.update(value.errors)
                if value.value is not None:
                    counts[f"parsed_{source}_rows"] += 1
        if row.message is not None and row.orderbook is not None:
            counts["paired_rows"] += 1
        msg = row.message.value if row.message else None
        book = row.orderbook.value if row.orderbook else None
        if msg is not None:
            reasons.update(_message_reasons(msg))
            events[msg.event_type] += 1
            time = msg.time_seconds
            if first_time is None:
                first_time = time
            last_time = time
            min_time = time if min_time is None else min(min_time, time)
            max_time = time if max_time is None else max(max_time, time)
            if previous_time is not None:
                if time < previous_time:
                    reasons.add("message.time_decrease")
                elif time == previous_time:
                    observations["time_ties"] += 1
            previous_time = time
            if msg.size == 0:
                observations["message_zero_size_rows"] += 1
            if msg.event_type == 7:
                observations["halt_marker_rows"] += 1
                if book is not None and previous_book is not None and book != previous_book:
                    reasons.add("orderbook.halt_state_changed")
        current_mid = None
        if book is not None:
            reasons.update(_book_reasons(book, observations))
            top = _top(book)
            if top is not None:
                ask, bid = top
                spread = ask - bid
                spreads[spread] += 1
                current_mid = ask + bid  # twice mid-price, in price units
                if spread == 0:
                    reasons.add("orderbook.locked")
                    observations["locked_rows"] += 1
                elif spread < 0:
                    reasons.add("orderbook.crossed")
                    observations["crossed_rows"] += 1
        if current_mid is not None and previous_mid is not None:
            mid_comparisons += 1
            mid_changes += current_mid != previous_mid
        previous_mid = current_mid
        previous_book = book
        if reasons:
            counts["suspicious_rows"] += 1
        for reason in sorted(reasons):
            issues[reason] += 1
            if len(examples[reason]) < 5:
                examples[reason].append(row.line)
    if counts["message_rows"] == counts["orderbook_rows"] == 0:
        issues["dataset.empty"] = 1
    return {
        "schema_version": 1,
        "inputs": {"message": str(message), "orderbook": str(orderbook), "levels": levels},
        "units": {"price": "0.0001 USD", "time": "exact decimal seconds since exchange-local midnight",
                  "size": "shares", "mid_comparison": "ask_price_1 + bid_price_1"},
        "counts": dict(counts),
        "row_counts_match": counts["message_rows"] == counts["orderbook_rows"],
        "time": {"min_ns": exact_ns(min_time) if min_time is not None else None,
                 "max_ns": exact_ns(max_time) if max_time is not None else None,
                 "min_seconds": str(min_time) if min_time is not None else None,
                 "max_seconds": str(max_time) if max_time is not None else None,
                 "first_seconds": str(first_time) if first_time is not None else None,
                 "last_seconds": str(last_time) if last_time is not None else None},
        "events": {str(k): {"name": EVENT_TYPES.get(k, "unknown"), "count": v}
                   for k, v in sorted(events.items())},
        "observations": dict(sorted(observations.items())),
        "spread": {"unit": "0.0001 USD", "weighting": "one observation per orderbook row",
                   "excluded_orderbook_rows": counts["orderbook_rows"] - sum(spreads.values()),
                   **_spread_summary(spreads)},
        "mid_price": {"changes": mid_changes, "adjacent_comparisons": mid_comparisons,
                      "excluded_adjacent_pairs": max(0, counts["orderbook_rows"] - 1) - mid_comparisons},
        "issues": {reason: {"row_count": count, "example_lines": examples[reason]}
                   for reason, count in sorted(issues.items())},
    }
