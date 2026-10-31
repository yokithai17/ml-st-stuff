import csv
import json
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from decimal import Decimal, localcontext
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from lob_lab.cli import main
from lob_lab.loader import iter_pairs, parse_integer, parse_seconds
from lob_lab.quality import quality_report
from lob_lab.sample import fetch_sample

FIXTURES = Path(__file__).parent / "fixtures"


class LoadingTests(unittest.TestCase):
    def test_prices_and_ids_never_pass_through_float(self):
        # Both exceed binary64's exact integer range.
        for value in ("9007199254740993", "9223372036854775807", "-9999999999"):
            self.assertEqual(str(parse_integer(value)), value)
        for value in ("100.0", "1e6", "NaN", "", " 100", "1_000"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_integer(value)

    def test_exact_time_even_beyond_documented_precision(self):
        with localcontext() as context:
            context.prec = 6
            time = parse_seconds("43002.632812672004")
        self.assertEqual(str(time), "43002.632812672004")
        self.assertLess(time, Decimal("43002.632812672005"))
        rows = list(iter_pairs(FIXTURES / "valid/message.csv", FIXTURES / "valid/orderbook.csv"))
        self.assertEqual(rows[0].message.value.time_ns, 34200000000001)

    def test_equal_times_preserve_original_order_and_raw_rows(self):
        rows = list(iter_pairs(FIXTURES / "valid/message.csv", FIXTURES / "valid/orderbook.csv"))
        self.assertEqual([r.line for r in rows], [1, 2, 3, 4, 5])
        self.assertEqual([r.message.value.event_type for r in rows[:2]], [1, 2])
        self.assertEqual(rows[1].orderbook.value.levels[0].ask_size, 5)
        self.assertTrue(rows[0].message.raw.startswith("34200.000000001,"))

    def test_bad_records_and_unmatched_tail_are_retained(self):
        rows = list(iter_pairs(FIXTURES / "anomalies/message.csv",
                               FIXTURES / "anomalies/orderbook.csv", 2))
        self.assertEqual(len(rows), 9)
        self.assertIsNone(rows[5].message.value)
        self.assertIn("message.integer_format", rows[5].message.errors)
        self.assertIn("orderbook.column_count", rows[6].orderbook.errors)
        self.assertIsNone(rows[-1].message)
        self.assertIsNotNone(rows[-1].orderbook.value)
        self.assertIsNone(rows[7].message.value.time_ns)


class QualityTests(unittest.TestCase):
    def test_known_valid_five_level_metrics(self):
        report = quality_report(FIXTURES / "valid/message.csv", FIXTURES / "valid/orderbook.csv")
        self.assertEqual(report["counts"]["message_rows"], 5)
        self.assertEqual(report["issues"], {})
        self.assertEqual(report["observations"]["time_ties"], 1)
        self.assertEqual(report["observations"]["ask_missing_levels"], 9)
        self.assertEqual(report["observations"]["message_zero_size_rows"], 1)
        self.assertEqual(report["spread"]["histogram"], {"100": 2, "200": 3})
        self.assertEqual(report["spread"]["mean"], "160")
        self.assertEqual(report["mid_price"]["changes"], 1)
        self.assertEqual(report["mid_price"]["adjacent_comparisons"], 4)

    def test_anomalies_are_counted_by_reason_and_source_line(self):
        report = quality_report(FIXTURES / "anomalies/message.csv",
                                FIXTURES / "anomalies/orderbook.csv", 2)
        self.assertFalse(report["row_counts_match"])
        self.assertEqual(report["counts"]["suspicious_rows"], 9)
        expected = {
            "orderbook.locked": [1], "orderbook.crossed": [2],
            "message.time_decrease": [3], "message.negative_size": [3],
            "orderbook.ask.negative_size": [3], "orderbook.ask.level_order": [3],
            "orderbook.bid.level_order": [3, 5], "orderbook.empty_side": [4, 5],
            "orderbook.halt_state_changed": [4], "message.unknown_event_type": [5],
            "message.zero_size": [5], "orderbook.ask.sentinel_nonzero_size": [5],
            "orderbook.ask.occupied_after_missing": [5], "orderbook.bid.invalid_price": [5],
            "orderbook.bid.zero_size_occupied": [5], "message.invalid_direction": [5],
            "orderbook.ask.zero_size_occupied": [5, 8], "message.integer_format": [6],
            "orderbook.column_count": [6, 7], "message.time_format": [7],
            "message.time_precision_gt_ns": [8], "pair.missing_message": [9],
        }
        self.assertEqual(set(report["issues"]), set(expected))
        for reason, lines in expected.items():
            with self.subTest(reason=reason):
                self.assertEqual(report["issues"][reason], {"row_count": len(lines), "example_lines": lines})
        self.assertEqual(report["spread"]["histogram"], {"-100": 1, "0": 1, "100": 1})
        self.assertEqual(report["mid_price"]["adjacent_comparisons"], 1)
        self.assertEqual(report["mid_price"]["excluded_adjacent_pairs"], 7)
        self.assertEqual(report["events"]["6"]["count"], 1)
        self.assertEqual(report["observations"]["both_sides_empty_rows"], 1)

    def audit_rows(self, messages, books, levels=1):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, rows in (("m", messages), ("b", books)):
                with (root / name).open("w", newline="") as handle:
                    csv.writer(handle).writerows(rows)
            return quality_report(root / "m", root / "b", levels)

    def test_all_three_halt_markers_and_seven_event_types(self):
        messages = [["34200", k, 1, 10, 100100, -1] for k in range(1, 7)]
        messages += [["34200", 7, 0, 0, price, -1] for price in (-1, 0, 1)]
        report = self.audit_rows(messages, [[100100, 10, 100000, 10]] * 9)
        self.assertEqual(report["issues"], {})
        self.assertEqual(set(report["events"]), set(map(str, range(1, 8))))

    def test_empty_files_and_missing_orderbook_tail(self):
        self.assertIn("dataset.empty", self.audit_rows([], [])["issues"])
        report = self.audit_rows([[34200, 1, 1, 10, 100100, -1]], [])
        self.assertEqual(report["issues"]["pair.missing_orderbook"]["row_count"], 1)
        self.assertIsNone(report["spread"]["p50"])

    def test_exact_subnanosecond_ordering_no_rounding(self):
        report = self.audit_rows(
            [[time, 1, 1, 1, 100100, -1] for time in
             ("34200.000000000002", "34200.000000000001")],
            [[100100, 10, 100000, 10]] * 2,
        )
        self.assertEqual(report["issues"]["message.time_decrease"]["example_lines"], [2])
        self.assertEqual(report["time"]["min_seconds"], "34200.000000000001")
        self.assertIsNone(report["time"]["min_ns"])

    def test_mid_sum_handles_half_units_and_offsetting_price_moves(self):
        report = self.audit_rows(
            [[34200, 1, 1, 1, 101, -1]] * 3,
            [[102, 10, 99, 10], [103, 10, 98, 10], [103, 10, 99, 10]],
        )
        self.assertEqual(report["mid_price"]["changes"], 1)
        self.assertEqual(report["spread"]["histogram"], {"3": 1, "4": 1, "5": 1})

    def test_one_empty_side_and_incorrect_halt_fields(self):
        report = self.audit_rows([[34200, 7, 1, 0, 10, 1]],
                                 [[9999999999, 0, 100000, 10]])
        self.assertIn("message.invalid_halt_marker", report["issues"])
        self.assertEqual(report["observations"]["ask_empty_rows"], 1)
        self.assertEqual(report["observations"]["both_sides_empty_rows"], 0)
        self.assertEqual(report["spread"]["excluded_orderbook_rows"], 1)

    def test_wrong_message_columns_and_five_level_book_width(self):
        report = self.audit_rows([[34200, 1, 1, 10, 100100]], [[100100, 10, 100000, 10]], 5)
        self.assertEqual(set(report["issues"]), {"message.column_count", "orderbook.column_count"})

    def test_malformed_quoted_line_does_not_swallow_next_record(self):
        with tempfile.TemporaryDirectory() as directory:
            message, book = Path(directory) / "m", Path(directory) / "b"
            message.write_text('"34200,1,1,10,100100,-1\n34201,1,2,10,100100,-1\n')
            book.write_text("100100,10,100000,10\n" * 2)
            report = quality_report(message, book, 1)
        self.assertEqual(report["counts"]["message_rows"], 2)
        self.assertEqual(report["events"]["1"]["count"], 1)
        self.assertEqual(report["issues"]["message.csv_syntax"]["example_lines"], [1])


class CommandTests(unittest.TestCase):
    def test_cli_writes_json_and_strict_exit_status(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(StringIO()):
            path = Path(directory) / "report.json"
            code = main(["report", "--message", str(FIXTURES / "anomalies/message.csv"),
                         "--orderbook", str(FIXTURES / "anomalies/orderbook.csv"),
                         "--levels", "2", "--strict", "--json", str(path)])
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(path.read_text())["counts"]["orderbook_rows"], 9)

    def test_no_overwriting_input_and_missing_input_exit(self):
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            with self.assertRaises(SystemExit) as error:
                main(["report", "--message", "m.csv", "--orderbook", "b.csv", "--json", "m.csv"])
            self.assertEqual(error.exception.code, 2)
            self.assertEqual(main(["report", "--message", "not-found.csv", "--orderbook", "missing.csv"]), 2)

    def test_corrupt_local_archive_is_retained_and_not_downloaded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = json.loads((Path(__file__).parent.parent / "data/manifest.json").read_text())
            archive = root / "data/raw" / manifest["archive_name"]
            archive.parent.mkdir(parents=True)
            archive.write_bytes(b"corrupt archive")
            with patch("urllib.request.urlopen") as network, self.assertRaisesRegex(ValueError, "SHA-256"):
                fetch_sample(root)
            network.assert_not_called()
            self.assertEqual(archive.read_bytes(), b"corrupt archive")


if __name__ == "__main__":
    unittest.main()
