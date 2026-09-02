# Timetable Parser

Parses timetable PDFs into structured JSON using positioned text from pdfplumber.

## Setup

Install dependencies:

```bash
pip install -r requirements.txt
```

Pass `--report path/to/report.json` when you want a parsing diagnostics report.

## Usage

```bash
python parse_timetable.py samples/newweek1.pdf -o samples/newweek1.parsed.json
```

The report is optional and is not created unless `--report` is supplied.
