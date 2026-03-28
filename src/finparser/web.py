"""FastAPI web app for finparser with real-time progress tracking via SSE."""

from __future__ import annotations

import asyncio
import json
import tempfile
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from finparser.agent import combine_results, extract_single
from finparser.excel import write_workbook
from finparser.parser import poll_result, upload_pdf_bytes

app = FastAPI(title="finparser")

STATIC_DIR = Path(__file__).parent / "static"
TEMPLATES_DIR = Path(__file__).parent / "templates"

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=TEMPLATES_DIR)

# In-memory job tracking
jobs: dict[str, dict] = {}


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html")


@app.post("/api/parse")
async def parse(
    llama_key: str = Form(...),
    gemini_key: str = Form(...),
    pdfs: list[UploadFile] = File(...),
    settings: str = Form(...),  # JSON string: list of per-PDF settings
):
    """Start the pipeline and return a job ID for progress tracking.

    Settings JSON format per PDF:
    {
        "start_page": int | null,
        "end_page": int | null,
        "specific_pages": str | null,
        "model": str
    }
    """
    try:
        pdf_settings = json.loads(settings)
    except json.JSONDecodeError:
        return JSONResponse({"error": "Invalid settings JSON"}, status_code=400)

    if len(pdfs) != len(pdf_settings):
        return JSONResponse(
            {"error": f"Got {len(pdfs)} PDFs but {len(pdf_settings)} settings entries"},
            status_code=400,
        )

    # Read all PDF contents upfront (UploadFile objects won't be accessible after response)
    uploads = []
    for i, pdf in enumerate(pdfs):
        content = await pdf.read()
        s = pdf_settings[i]
        uploads.append({
            "filename": pdf.filename,
            "content": content,
            "start_page": s.get("start_page"),
            "end_page": s.get("end_page"),
            "specific_pages": s.get("specific_pages"),
        })

    models_per_pdf = [s.get("model", "gemini-2.5-pro") for s in pdf_settings]

    job_id = str(uuid.uuid4())
    jobs[job_id] = {
        "status": "running",
        "progress": 0,
        "message": "Starting...",
        "file_path": None,
        "error": None,
    }

    asyncio.create_task(
        _run_pipeline(job_id, uploads, llama_key, gemini_key, models_per_pdf)
    )

    return JSONResponse({"job_id": job_id})


@app.get("/api/progress/{job_id}")
async def progress(job_id: str):
    """SSE endpoint streaming progress updates for a parse job."""
    if job_id not in jobs:
        return JSONResponse({"error": "Job not found"}, status_code=404)

    async def event_stream():
        last_sent = None
        while True:
            job = jobs.get(job_id)
            if not job:
                yield f"data: {json.dumps({'status': 'error', 'progress': 0, 'message': 'Job not found'})}\n\n"
                break

            current = (job["status"], job["progress"], job["message"])
            if current != last_sent:
                yield f"data: {json.dumps({'status': job['status'], 'progress': job['progress'], 'message': job['message']})}\n\n"
                last_sent = current

            if job["status"] in ("done", "error"):
                break

            await asyncio.sleep(0.3)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/download/{job_id}")
async def download(job_id: str):
    """Download the generated Excel file for a completed job."""
    job = jobs.get(job_id)
    if not job:
        return JSONResponse({"error": "Job not found"}, status_code=404)
    if job["status"] != "done" or not job["file_path"]:
        return JSONResponse({"error": "File not ready"}, status_code=404)

    return FileResponse(
        Path(job["file_path"]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename="finparser_output.xlsx",
    )


async def _run_pipeline(
    job_id: str,
    uploads: list[dict],
    llama_key: str,
    gemini_key: str,
    models_per_pdf: list[str],
):
    """Run the full parse pipeline, updating job progress along the way.

    Progress breakdown (approximate):
        0-10%   Upload PDFs to LlamaParse
        10-50%  LlamaParse processing (polling)
        50-80%  Gemini extraction (per-PDF)
        80-90%  Gemini combine pass (multi-PDF only)
        90-100% Excel generation
    """
    job = jobs[job_id]
    n = len(uploads)

    def set_progress(pct: int, msg: str):
        job["progress"] = min(pct, 99) if pct < 100 else 100
        job["message"] = msg

    try:
        # --- Stage 1: Upload PDFs to LlamaParse ---
        set_progress(2, f"Uploading {n} PDF(s) to LlamaParse...")

        import httpx

        async with httpx.AsyncClient() as client:
            job_ids = []
            for i, u in enumerate(uploads):

                # post request to LlamaParse API
                jid = await upload_pdf_bytes(
                    client,
                    u["filename"],
                    u["content"],
                    llama_key,
                    start_page=u.get("start_page"),
                    end_page=u.get("end_page"),
                    specific_pages=u.get("specific_pages"),
                )
                # an upload is finished
                job_ids.append(jid)
                set_progress(
                    2 + int(8 * (i + 1) / n),
                    f"Uploaded {i + 1}/{n} to LlamaParse",
                )

            # --- Stage 2: Poll LlamaParse until complete ---
            set_progress(10, f"Waiting for LlamaParse to process {n} PDF(s)...")

            markdowns: list[str | None] = [None] * n
            completed_count = 0

            async def poll_one(idx: int, jid: str):
                nonlocal completed_count
                markdowns[idx] = await poll_result(client, jid, llama_key)
                completed_count += 1
                set_progress(
                    10 + int(40 * completed_count / n),
                    f"LlamaParse: {completed_count}/{n} PDF(s) processed",
                )

            # asynchronously poll every job id without waiting on one. Await suspends program until all coroutines are complete
            await asyncio.gather(*[poll_one(i, jid) for i, jid in enumerate(job_ids)])

        # --- Stage 3: Gemini extraction (per-PDF) ---
        extracted = []
        for i, md in enumerate(markdowns):
            model = models_per_pdf[i] if i < len(models_per_pdf) else "gemini-2.5-pro"
            set_progress(
                50 + int(30 * i / max(n, 1)),
                f"Extracting financial data from PDF {i + 1}/{n}...",
            )
            result = await asyncio.to_thread(extract_single, md, gemini_key, model)
            extracted.append(result)
            set_progress(
                50 + int(30 * (i + 1) / n),
                f"Extracted {i + 1}/{n} PDF(s)",
            )

        # --- Stage 4: Combine pass (multi-PDF only) ---
        if n > 1:
            set_progress(80, "Combining results across documents...")
            final_result = await asyncio.to_thread(
                combine_results, extracted, gemini_key
            )
            set_progress(90, "Results combined")
        else:
            final_result = extracted[0]
            set_progress(90, "Extraction complete")

        # --- Stage 5: Generate Excel ---
        set_progress(92, "Generating Excel workbook...")
        tmp = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
        tmp.close()
        output_path = Path(tmp.name)
        await asyncio.to_thread(write_workbook, final_result, output_path)

        job["file_path"] = str(output_path)
        set_progress(100, "Done!")
        job["status"] = "done"

    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)
        job["progress"] = 0
        job["message"] = str(e)
