# Timetable Parser

Parses timetable PDFs into structured JSON using Camelot and pdfplumber.

## Setup

Install dependencies:

```bash
pip install -r requirements.txt
```

> Note: `camelot-py` requires Ghostscript and additional system packages on some platforms.

## Usage

```bash
python parse_timetable.py samples/timetable.pdf -o samples/timetable.parsed.json
```

To keep truncated program lines (useful for matching `samples/week1.json`):

```bash
python parse_timetable.py samples/timetable.pdf -o samples/timetable.parsed.json --keep-truncated
```

To disable resolution from the first page:

```bash
python parse_timetable.py samples/timetable.pdf --no-resolve-truncated
```
