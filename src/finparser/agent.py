"""Gemini agent — extracts structured financial data from markdown via JSON mode."""

from __future__ import annotations

from google import genai
from google.genai import types

from finparser.models import ParseResult

SYSTEM_PROMPT = """\
You are a financial document parser. You receive markdown text extracted from \
financial statement PDFs (balance sheets, income statements, cash flow statements, etc.).

Your job is to extract structured data as JSON. For each financial statement you \
find in the markdown:

1. Identify the statement type (balance_sheet, income_statement, cash_flow).
2. Identify the period columns (e.g. "2024", "2023", "Q3 2024").
3. Extract every line item preserving:
   - The original label exactly as written (do NOT rename or normalize).
   - The hierarchy depth (indent level: 0 for section headers, 1 for items, 2 for sub-items).
   - Numeric values for each period (use null for missing/blank values).
   - Whether the row is a total/subtotal line.
4. Clean up obvious OCR artifacts (extra spaces, garbled characters) but do NOT \
   change the meaning or wording of labels.

Return ALL financial statements found in a single JSON response matching this schema:

{
  "statements": [
    {
      "title": "string — statement title as-is from document",
      "statement_type": "balance_sheet | income_statement | cash_flow | other",
      "periods": ["2024", "2023"],
      "line_items": [
        {
          "label": "string — original label from PDF",
          "indent": 0,
          "values": {"2024": 123456, "2023": 789012},
          "is_total": false
        }
      ]
    }
  ]
}"""

COMBINE_PROMPT = """\
You are a financial data combiner. You receive structured JSON data extracted \
from multiple financial statement PDFs. Your job is to merge them into a single \
coherent result.

Rules:
- Keep all statements from all documents.
- If two documents contain the same statement type for the same entity/periods, \
  merge them into one statement (combine line items, avoid duplicates).
- Preserve original labels and hierarchy exactly.
- In the line_items values, where a value can be computed as a sum of other rows \
  in the same statement, note this by setting is_total to true.

Return the merged result as JSON matching this schema:

{
  "statements": [
    {
      "title": "string",
      "statement_type": "balance_sheet | income_statement | cash_flow | other",
      "periods": ["2024", "2023"],
      "line_items": [
        {
          "label": "string",
          "indent": 0,
          "values": {"2024": 123456, "2023": 789012},
          "is_total": false
        }
      ]
    }
  ]
}"""


def extract_single(
    markdown: str,
    api_key: str,
    model: str = "gemini-2.5-pro",
) -> ParseResult:
    """Extract financial statements from a single PDF's markdown."""
    client = genai.Client(api_key=api_key)

    response = client.models.generate_content(
        model=model,
        contents=f"Extract all financial statements from the following markdown.\n\n{markdown}",
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            response_mime_type="application/json",
        ),
    )

    return ParseResult.model_validate_json(response.text)


def combine_results(
    results: list[ParseResult],
    api_key: str,
    model: str = "gemini-2.5-pro",
) -> ParseResult:
    """Combine multiple ParseResults into one via a Gemini pass."""
    if len(results) == 1:
        return results[0]

    client = genai.Client(api_key=api_key)

    # Serialize each result
    docs = "\n\n---\n\n".join(
        f"## Document {i + 1}\n\n{r.model_dump_json(indent=2)}"
        for i, r in enumerate(results)
    )

    response = client.models.generate_content(
        model=model,
        contents=f"Combine the following extracted financial data into a single result.\n\n{docs}",
        config=types.GenerateContentConfig(
            system_instruction=COMBINE_PROMPT,
            response_mime_type="application/json",
        ),
    )

    return ParseResult.model_validate_json(response.text)


def extract_statements(
    markdowns: list[str],
    api_key: str,
    model: str = "gemini-2.5-pro",
    models_per_pdf: list[str] | None = None,
) -> ParseResult:
    """Extract and combine financial statements from multiple PDFs.

    Strategy:
    1. Process each PDF's markdown independently (avoids context overload).
    2. If multiple PDFs, run a combine pass to merge results.

    Args:
        markdowns: List of markdown strings (one per PDF).
        api_key: Google AI API key.
        model: Default Gemini model ID.
        models_per_pdf: Optional per-PDF model override list.

    Returns:
        ParseResult containing all extracted FinancialStatement objects.
    """
    results = []
    for i, md in enumerate(markdowns):
        pdf_model = models_per_pdf[i] if models_per_pdf and i < len(models_per_pdf) else model
        results.append(extract_single(md, api_key, model=pdf_model))

    return combine_results(results, api_key, model=model)
