"""LlamaParse API integration — upload PDFs and retrieve markdown."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx

LLAMAPARSE_BASE = "https://api.cloud.llamaindex.ai/api/v2/parse"
POLL_INTERVAL = 2  # seconds between status checks
MAX_POLL_ATTEMPTS = 150  # 5 minutes at 2s intervals


def _build_config(
    start_page: int | None = None,
    end_page: int | None = None,
    specific_pages: str | None = None,
) -> dict:
    """Build the LlamaParse configuration dict with optional page ranges.

    Page selection is XOR: use (start_page / end_page) OR specific_pages, not both.
    specific_pages accepts formats like "1,2,6-9,15".
    """
    config: dict = {
        "tier": "cost_effective",
        "version": "latest",
    }
    if specific_pages:
        config["page_ranges"] = {"target_pages": specific_pages}
    elif start_page is not None or end_page is not None:
        lo = start_page or 1
        hi = end_page
        target = f"{lo}-{hi}" if hi and hi != lo else str(lo) if not hi else f"{lo}-{hi}"
        config["page_ranges"] = {"target_pages": target}
    return config


async def upload_pdf(
    client: httpx.AsyncClient,
    pdf_path: Path,
    api_key: str,
    start_page: int | None = None,
    end_page: int | None = None,
    specific_pages: str | None = None,
) -> str:
    """Upload a PDF to LlamaParse and return the job ID."""
    headers = {"Authorization": f"Bearer {api_key}"}
    config = _build_config(start_page, end_page, specific_pages)

    with open(pdf_path, "rb") as f:
        files: dict = {
            "file": (pdf_path.name, f, "application/pdf"),
            "configuration": (None, json.dumps(config)),
        }
        resp = await client.post(
            f"{LLAMAPARSE_BASE}/upload",
            headers=headers,
            files=files,
            timeout=60,
        )
    resp.raise_for_status()
    return resp.json()["id"]


async def upload_pdf_bytes(
    client: httpx.AsyncClient,
    filename: str,
    content: bytes,
    api_key: str,
    start_page: int | None = None,
    end_page: int | None = None,
    specific_pages: str | None = None,
) -> str:
    """Upload PDF bytes (from web upload) to LlamaParse and return the job ID."""
    headers = {"Authorization": f"Bearer {api_key}"}
    config = _build_config(start_page, end_page, specific_pages)

    files: dict = {
        "file": (filename, content, "application/pdf"),
        "configuration": (None, json.dumps(config)),
    }
    resp = await client.post(
        f"{LLAMAPARSE_BASE}/upload", # post request to llamaparse
        headers=headers,
        files=files,
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["id"]


async def poll_result(
    client: httpx.AsyncClient,
    job_id: str,
    api_key: str,
) -> str:
    """Poll a LlamaParse job until completion and return the combined markdown."""
    headers = {"Authorization": f"Bearer {api_key}"}

    for _ in range(MAX_POLL_ATTEMPTS):
        resp = await client.get(
            f"{LLAMAPARSE_BASE}/{job_id}",
            headers=headers,
            params={"expand": "markdown"},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        status = data["job"]["status"]

        if status == "COMPLETED":
            pages = data["markdown"]["pages"]
            return "\n\n".join(p["markdown"] for p in pages)
        elif status in ("FAILED", "CANCELLED"):
            error = data["job"].get("error_message", "unknown error")
            raise RuntimeError(f"LlamaParse job {job_id} {status}: {error}")

        # job is PENDING/PROCESSING

        await asyncio.sleep(POLL_INTERVAL)

    raise TimeoutError(f"LlamaParse job {job_id} did not complete in time")


async def parse_pdf(
    pdf_path: Path,
    api_key: str,
    start_page: int | None = None,
    end_page: int | None = None,
    specific_pages: str | None = None,
) -> str:
    """Parse a single PDF and return its markdown content."""
    async with httpx.AsyncClient() as client:
        job_id = await upload_pdf(client, pdf_path, api_key, start_page, end_page, specific_pages)
        return await poll_result(client, job_id, api_key)


async def parse_pdfs(
    pdf_specs: list[tuple[Path, int | None, int | None]],
    api_key: str,
) -> list[str]:
    """Parse multiple PDFs concurrently (CLI interface — start/end page only).

    Args:
        pdf_specs: List of (pdf_path, start_page, end_page) tuples.
        api_key: LlamaCloud API key.

    Returns:
        List of markdown strings, one per PDF, in the same order as input.
    """
    async with httpx.AsyncClient() as client:
        upload_tasks = [
            upload_pdf(client, path, api_key, start, end)
            for path, start, end in pdf_specs
        ]
        job_ids = await asyncio.gather(*upload_tasks)
        poll_tasks = [poll_result(client, jid, api_key) for jid in job_ids]
        return list(await asyncio.gather(*poll_tasks))


async def parse_pdf_uploads(
    uploads: list[dict],
    api_key: str,
) -> list[str]:
    """Parse multiple PDF uploads concurrently (web interface).

    Args:
        uploads: List of dicts with keys: filename, content (bytes),
                 and optionally start_page, end_page, specific_pages.
        api_key: LlamaCloud API key.

    Returns:
        List of markdown strings, one per PDF.
    """
    async with httpx.AsyncClient() as client:
        upload_tasks = [
            upload_pdf_bytes(
                client,
                u["filename"],
                u["content"],
                api_key,
                start_page=u.get("start_page"),
                end_page=u.get("end_page"),
                specific_pages=u.get("specific_pages"),
            )
            for u in uploads
        ]
        job_ids = await asyncio.gather(*upload_tasks)
        poll_tasks = [poll_result(client, jid, api_key) for jid in job_ids]
        return list(await asyncio.gather(*poll_tasks))
