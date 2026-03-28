# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**finparser** — A pipeline that converts financial statement PDFs into structured Excel workbooks.

**Flow:** PDF upload → LlamaParse API (PDF→Markdown) → Gemini agent (Markdown→structured data) → Excel output (openpyxl)

Users can upload one or more PDFs with per-PDF page range settings. The Gemini agent processes each PDF's markdown independently, then a final pass combines all statements into a single Excel workbook with appropriate sheets and formulas.

**Target users:** Finance team members at Lee Family Office. Most are not technical — the UI must require zero CLI/config knowledge. API keys are entered in the web UI, not in .env files.

## Tech Stack

- **Python 3.11+** (managed with `uv`)
- **LlamaParse** — PDF-to-Markdown via LlamaCloud REST API (httpx). Docs exposed via llama-index-docs MCP
- **Google GenAI SDK** (`google-genai`) — Gemini agent for structuring/combining parsed output
- **openpyxl** — Excel workbook generation
- **Click** — CLI interface (dev/power-user access)
- **FastAPI** — Web app backend
- **Frontend** — TBD (lightweight, see UI section below)

## Project Structure

```
finparser/
├── src/finparser/
│   ├── __init__.py
│   ├── cli.py          # Click CLI entry point
│   ├── parser.py       # LlamaParse REST API integration (httpx, async)
│   ├── agent.py        # Gemini agent — markdown → structured financial data (JSON mode)
│   ├── excel.py        # Excel workbook generation via openpyxl
│   └── models.py       # Pydantic data models for financial statements
├── tests/
├── parsed/             # Cached LlamaParse markdown output (auto-generated)
├── .env                # API keys (LLAMA_CLOUD_API_KEY, GOOGLE_API_KEY) — dev only
└── pyproject.toml
```

## Commands

```bash
# Install
uv sync              # install all deps
uv sync --all-extras # include dev deps

# Run CLI
uv run finparser parse statement.pdf
uv run finparser parse statement.pdf --start-page 3 --end-page 10
uv run finparser parse bs.pdf is.pdf cf.pdf -o output.xlsx

# Use cached LlamaParse output (skip re-parsing)
uv run finparser parse statement.pdf --cached
uv run finparser parse statement.pdf --cached --max-chars 5000  # truncate for testing

# Test
uv run pytest
uv run pytest tests/test_parser.py -v
uv run pytest -k "test_parse_balance_sheet"

# Lint
uv run ruff check src/
uv run ruff format src/
```

## Architecture Notes

- **parser.py** sends PDFs to LlamaParse with page range params, returns markdown strings. Each PDF is parsed independently and concurrently via asyncio. Parsed markdown is cached to `parsed/` for reuse.
- **agent.py** receives markdown outputs and calls Gemini API in **JSON mode** to extract structured data. Returns JSON conforming to a generic table schema — no rigid per-statement-type fields.
- **excel.py** takes structured models and writes a formatted Excel workbook — one sheet per statement found. Output mirrors the source PDF's structure (labels, hierarchy, periods) rather than forcing a canonical template.
- **models.py** defines generic Pydantic models that work across any financial statement format.

### Data Model Design

Financial statements vary widely across companies — different line items, naming, hierarchy. The models are intentionally **schema-agnostic**: they represent tables, not canonical financial structures.

```python
class LineItem:
    label: str                        # original label from PDF, e.g. "Net Sales"
    indent: int                       # hierarchy depth (0=header, 1=item, 2=sub-item)
    values: dict[str, float | None]   # period → value, e.g. {"2024": 394328}
    is_total: bool                    # distinguishes subtotals/totals from regular items

class FinancialStatement:
    title: str           # as-is from document, e.g. "Consolidated Balance Sheet"
    statement_type: str  # "balance_sheet" | "income_statement" | "cash_flow" | "other"
    periods: list[str]   # column headers, e.g. ["2024", "2023"]
    line_items: list[LineItem]
```

The agent's role is NOT to normalize labels (e.g. "Net Sales" → "Revenue") but to:
1. Identify statement types and period columns from the markdown
2. Extract line items preserving original labels and hierarchy
3. Clean up OCR/parsing artifacts from LlamaParse

### Agent Strategy (Multi-PDF)

For multiple PDFs, context may be too large for a single prompt. The strategy is:
1. **Per-PDF extraction** — Process each PDF's markdown independently through Gemini, producing `FinancialStatement` objects for each.
2. **Combine pass** — A final Gemini call receives all extracted statements and produces the combined workbook structure **with Excel formulas** linking related data across statements.

### Agent → Excel Data Flow

```
Markdown(s) → Gemini API (JSON mode) → JSON → Pydantic models → openpyxl
```

Excel writer maps each `FinancialStatement` to a sheet: column A = labels (indented by `indent`), columns B+ = period values, bold on `is_total` rows.

## Environment Variables

Loaded from `.env` via python-dotenv (CLI/dev mode only):
- `LLAMA_CLOUD_API_KEY` — LlamaCloud API key for LlamaParse
- `GOOGLE_API_KEY` — Google AI API key for Gemini

In the web app, users provide their own API keys via the UI — no .env required.

## Web UI Design

See `ui-mockup.png` for visual reference.

### Layout

**Left panel** — PDF preview card (large, rounded). Shows the currently selected PDF. Below it: filename label, pagination dots (one per uploaded PDF), and back/forward navigation arrows.

**Right panel** — Per-PDF parameter inputs. These are **per-PDF** (each PDF in the carousel has its own settings):

- **Starting Page** / **Ending Page** — optional number inputs, side by side
- **Page nums** — text input for specific pages (e.g. "1,2,6-9"). This is **XOR** with Starting/Ending Page — user picks one mode or the other
- **Model** — dropdown selector. Default: Gemini 2.5 Pro. Options include available Gemini models

### Flow

1. User enters their API keys (Gemini + LlamaParse) in the UI — stored in session only, never persisted
2. User clicks "Upload PDF(s)" — uploads one or more PDFs (with a reasonable limit)
3. Carousel appears with PDF preview + per-PDF parameter controls
4. User navigates between PDFs, sets page ranges and model per PDF
5. User clicks "Parse" — pipeline runs (LlamaParse → Gemini → Excel)
6. Excel file is offered for download

## Development Phases

1. **CLI prototype** (complete) — end-to-end pipeline via command line
2. **Web MVP** (current) — FastAPI backend + lightweight frontend with PDF carousel UI
