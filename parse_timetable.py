from __future__ import annotations

import argparse
import json
from pathlib import Path

from parser import parse_timetable_with_report


def main() -> None:
    parser = argparse.ArgumentParser(description="Parse timetable PDFs into JSON.")
    parser.add_argument("pdf_path", help="Path to the timetable PDF")
    parser.add_argument(
        "-o",
        "--output",
        help="Path to write JSON output. Defaults to <pdf>.json",
    )
    parser.add_argument(
        "--report",
        help="Path to write the parsing report. Defaults to <output>.report.json",
    )

    args = parser.parse_args()
    pdf_path = Path(args.pdf_path)
    output_path = (
        Path(args.output) if args.output else pdf_path.with_suffix(".json")
    )

    timetable, report = parse_timetable_with_report(str(pdf_path))

    output_path.write_text(
        json.dumps(timetable, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    if args.report:
        Path(args.report).write_text(
            json.dumps(report, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
