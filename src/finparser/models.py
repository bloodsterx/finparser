"""Pydantic data models for financial statements.

Schema-agnostic: represents tables, not canonical financial structures.
Works across any financial statement format (10-K, P&L, balance sheet, etc.).
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class LineItem(BaseModel):
    """A single row in a financial statement."""

    label: str = Field(description="Original label from the PDF, e.g. 'Net Sales'")
    indent: int = Field(
        default=0,
        description="Hierarchy depth: 0=header/section, 1=item, 2=sub-item, etc.",
    )
    values: dict[str, float | str | None] = Field(
        default_factory=dict,
        description="Column header → value, e.g. {'2024': 394328} or {'Country': 'UK'}",
    )
    is_total: bool = Field(
        default=False,
        description="True for subtotal/total rows, false for regular line items",
    )


class FinancialStatement(BaseModel):
    """A single financial statement extracted from a document."""

    title: str = Field(
        description="Statement title as-is from the document, e.g. 'Consolidated Balance Sheet'"
    )
    statement_type: str = Field(
        description="One of: balance_sheet, income_statement, cash_flow, other"
    )
    periods: list[str] = Field(
        description="Column headers for time periods, e.g. ['2024', '2023']"
    )
    line_items: list[LineItem] = Field(
        default_factory=list,
        description="Rows of the statement, preserving original order and hierarchy",
    )
    # notes: dict[LineItem, int] = Field(
    #     default=list,
    #     description="Each line-item's reference to the financial statement notes note to the financial statements"
    # )


class ParseResult(BaseModel):
    """Collection of financial statements extracted from one or more PDFs."""

    statements: list[FinancialStatement] = Field(default_factory=list)
