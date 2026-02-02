from __future__ import annotations

import argparse
import json
from pathlib import Path

from parser import parse_timetable


def main() -> None:
    parser = argparse.ArgumentParser(description="Parse timetable PDFs into JSON.")
    parser.add_argument("pdf_path", help="Path to the timetable PDF")
    parser.add_argument(
        "-o",
        "--output",
        help="Path to write JSON output. Defaults to <pdf>.json",
    )
    parser.add_argument(
        "--keep-truncated",
        action="store_true",
        help="Keep truncated program lines even if a full match is found.",
    )
    parser.add_argument(
        "--no-resolve-truncated",
        action="store_true",
        help="Disable resolving truncated program lines from the first page.",
    )

    args = parser.parse_args()
    pdf_path = Path(args.pdf_path)
    output_path = (
        Path(args.output) if args.output else pdf_path.with_suffix(".json")
    )

    timetable = parse_timetable(
        str(pdf_path),
        resolve_truncated=not args.no_resolve_truncated,
        keep_truncated=args.keep_truncated,
    )

    output_path.write_text(
        json.dumps(timetable, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
