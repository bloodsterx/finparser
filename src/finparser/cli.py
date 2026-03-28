"""Click CLI entry point for finparser."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import click
from dotenv import load_dotenv

load_dotenv()

CACHE_DIR = Path("parsed")


def _cache_path(pdf_path: Path) -> Path:
    """Return the cache file path for a given PDF."""
    return CACHE_DIR / f"{pdf_path.stem}.md"


def _save_cache(pdf_path: Path, markdown: str) -> None:
    """Save parsed markdown to the cache directory."""
    CACHE_DIR.mkdir(exist_ok=True)
    _cache_path(pdf_path).write_text(markdown, encoding="utf-8")


def _load_cache(pdf_path: Path) -> str:
    """Load cached markdown for a PDF."""
    path = _cache_path(pdf_path)
    if not path.exists():
        click.echo(f"Error: No cached markdown for {pdf_path.name} at {path}", err=True)
        sys.exit(1)
    return path.read_text(encoding="utf-8")


@click.group()
def cli() -> None:
    """finparser — Convert financial statement PDFs into structured Excel workbooks."""


@cli.command()
@click.argument("pdfs", nargs=-1, required=True, type=click.Path(exists=True, path_type=Path))
@click.option("-o", "--output", "output_path", default=None, type=click.Path(path_type=Path),
              help="Output Excel file path. Defaults to 'output.xlsx'.")
@click.option("--start-page", type=int, default=None, help="Start page (1-based, applies to all PDFs).")
@click.option("--end-page", type=int, default=None, help="End page (1-based, applies to all PDFs).")
@click.option("--model", default="gemini-2.5-pro", help="Gemini model to use for extraction.")
@click.option("--cached", is_flag=True, help="Skip LlamaParse and use cached markdown from parsed/ dir.")
@click.option("--max-chars", type=int, default=None,
              help="Truncate markdown to this many characters before sending to Gemini (for testing).")
def parse(
    pdfs: tuple[Path, ...],
    output_path: Path | None,
    start_page: int | None,
    end_page: int | None,
    model: str,
    cached: bool,
    max_chars: int | None,
) -> None:
    """Parse financial statement PDFs into an Excel workbook.

    Accepts one or more PDF files. Each PDF is sent to LlamaParse for markdown
    extraction, then Gemini structures the data into an Excel workbook.

    Use --cached to skip LlamaParse and reuse previously saved markdown from parsed/.
    """
    from finparser.agent import extract_statements
    from finparser.excel import write_workbook
    from finparser.parser import parse_pdfs

    gemini_key = os.environ.get("GOOGLE_API_KEY")
    if not gemini_key:
        click.echo("Error: GOOGLE_API_KEY not set in environment or .env", err=True)
        sys.exit(1)

    if output_path is None:
        output_path = Path("output.xlsx")

    # Step 1: Get markdown — either from cache or LlamaParse
    if cached:
        click.echo(f"Loading cached markdown for {len(pdfs)} PDF(s)...")
        markdowns = [_load_cache(pdf) for pdf in pdfs]
        for i, md in enumerate(markdowns):
            click.echo(f"  ✓ {pdfs[i].name} — {len(md):,} chars (cached)")
    else:
        llama_key = os.environ.get("LLAMA_CLOUD_API_KEY")
        if not llama_key:
            click.echo("Error: LLAMA_CLOUD_API_KEY not set in environment or .env", err=True)
            sys.exit(1)

        pdf_specs = [(pdf, start_page, end_page) for pdf in pdfs]
        click.echo(f"Parsing {len(pdfs)} PDF(s) via LlamaParse...")
        markdowns = asyncio.run(parse_pdfs(pdf_specs, llama_key))

        for i, md in enumerate(markdowns):
            click.echo(f"  ✓ {pdfs[i].name} — {len(md):,} chars of markdown")
            _save_cache(pdfs[i], md)

        click.echo(f"  Cached markdown to {CACHE_DIR}/")

    # Truncate if --max-chars is set
    if max_chars:
        markdowns = [md[:max_chars] for md in markdowns]
        click.echo(f"  Truncated to {max_chars:,} chars per document")

    # Step 2: Extract structured data via Gemini
    click.echo(f"Extracting financial data via Gemini ({model})...")
    result = extract_statements(markdowns, gemini_key, model=model)
    click.echo(f"  ✓ Found {len(result.statements)} statement(s)")

    for stmt in result.statements:
        click.echo(f"    - {stmt.title} ({stmt.statement_type}, {len(stmt.line_items)} rows)")

    # Step 3: Write Excel
    click.echo(f"Writing {output_path}...")
    write_workbook(result, output_path)
    click.echo(f"  ✓ Done: {output_path}")
