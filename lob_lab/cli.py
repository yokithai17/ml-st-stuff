import argparse
import json
import sys
from pathlib import Path
from urllib.error import URLError
from zipfile import BadZipFile

from .quality import quality_report
from .sample import fetch_sample, sample_paths


def render_markdown(report: dict) -> str:
    counts, time, spread = report["counts"], report["time"], report["spread"]
    lines = [
        "# LOB Lab — отчёт о качестве данных", "",
        f"Глубина: {report['inputs']['levels']}. Единица цены: 0,0001 USD; объём: акции.",
        "Статистики взвешены по строкам, не по времени. Порядок исходный.", "",
        f"Строк message: **{counts['message_rows']}**, orderbook: **{counts['orderbook_rows']}**.",
        f"Число строк совпадает: **{report['row_counts_match']}**. Пар строк: {counts['paired_rows']}.",
        f"Разобрано message: {counts['parsed_message_rows']}, orderbook: {counts['parsed_orderbook_rows']}.",
        f"Подозрительных позиций: **{counts['suspicious_rows']}** (причины могут пересекаться).", "",
        f"Время: **{time['min_seconds']} — {time['max_seconds']}** секунд от локальной полуночи биржи.",
        f"Совпадений времени: {report['observations'].get('time_ties', 0)}.", "",
        "## Типы событий", "", "| Тип | Название | Строк |", "| --- | --- | ---: |",
    ]
    for kind, event in report["events"].items():
        lines.append(f"| {kind} | {event['name']} | {event['count']} |")
    lines += ["", "## Спред и mid-price", "",
              f"Наблюдений спреда: {spread['count']}; исключено строк стакана: {spread['excluded_orderbook_rows']}.",
              f"Спред в единицах 0,0001 USD: min={spread['min']}, p50={spread['p50']}, "
              f"p95={spread['p95']}, p99={spread['p99']}, max={spread['max']}; mean={spread['mean']}.",
              "Квантили: ближайший ранг (nearest rank). Locked/crossed включены, если лучшие уровни корректны.",
              f"Изменений mid-price: **{report['mid_price']['changes']}** из "
              f"{report['mid_price']['adjacent_comparisons']} соседних сравнений; "
              f"исключено пар: {report['mid_price']['excluded_adjacent_pairs']}.",
              "Mid сравнивается точно через сумму ask+bid; через пропуски сравнения не строятся.", "",
              "### Распределение спреда", "", "| Спред | Строк |", "| ---: | ---: |"]
    for value, count in spread["histogram"].items():
        lines.append(f"| {value} | {count} |")
    lines += ["", "## Причины замечаний", "",
              "Номера строк начинаются с 1; для каждой причины показаны первые пять позиций.", "",
              "| Причина | Строк / случаев | Примеры строк |", "| --- | ---: | --- |"]
    for reason, issue in report["issues"].items():
        lines.append(f"| {reason} | {issue['row_count']} | {', '.join(map(str, issue['example_lines']))} |")
    if not report["issues"]:
        lines.append("| Замечаний нет | 0 | |")
    lines += ["", "## Дополнительные счётчики", "", "| Наблюдение | Количество |", "| --- | ---: |"]
    for name, value in report["observations"].items():
        lines.append(f"| {name} | {value} |")
    lines += ["", "Отсутствие причины означает нулевой счётчик. Допустимые sentinel+0 и нули "
              "служебных событий учитываются в дополнительных счётчиках.",
              "Строки не удалялись и не исправлялись. Правила метрик и проверок: docs/data-schema.md.", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LOB Lab: загрузка и аудит LOBSTER CSV")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("fetch", help="скачать/проверить официальный образец и распаковать локально")
    report_parser = commands.add_parser("report", help="построить отчёт без изменения CSV")
    report_parser.add_argument("--sample", action="store_true", help="официальный локальный образец AMZN")
    report_parser.add_argument("--message", type=Path)
    report_parser.add_argument("--orderbook", type=Path)
    report_parser.add_argument("--levels", type=int, default=5)
    report_parser.add_argument("--json", type=Path, dest="json_path")
    report_parser.add_argument("--markdown", type=Path)
    report_parser.add_argument("--strict", action="store_true", help="код 1 при любых замечаниях")
    args = parser.parse_args(argv)
    try:
        if args.command == "fetch":
            for path in fetch_sample():
                print(path)
            return 0
        if args.levels < 1:
            parser.error("--levels должен быть положительным")
        if args.sample:
            if args.message or args.orderbook or args.levels != 5:
                parser.error("--sample задаёт оба файла и глубину 5")
            message, orderbook = sample_paths()
            # Keep reports portable when run from the documented repository root.
            if Path.cwd() in message.parents and Path.cwd() in orderbook.parents:
                message = message.relative_to(Path.cwd())
                orderbook = orderbook.relative_to(Path.cwd())
        else:
            if args.message is None or args.orderbook is None:
                parser.error("задайте --sample либо --message и --orderbook")
            message, orderbook = args.message, args.orderbook
        outputs = [path.resolve() for path in (args.json_path, args.markdown) if path is not None]
        if len(outputs) != len(set(outputs)) or set(outputs) & {message.resolve(), orderbook.resolve()}:
            parser.error("пути отчётов должны различаться и не совпадать с исходными CSV")
        result = quality_report(message, orderbook, args.levels)
        markdown = render_markdown(result)
        for path, content in (
            (args.json_path, json.dumps(result, ensure_ascii=False, indent=2) + "\n"),
            (args.markdown, markdown),
        ):
            if path is not None:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
        print(markdown, end="")
        return 1 if args.strict and result["issues"] else 0
    except (OSError, ValueError, URLError, BadZipFile) as error:
        print(f"lob-lab: {error}", file=sys.stderr)
        return 2
