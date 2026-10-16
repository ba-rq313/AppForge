from __future__ import annotations

import shutil
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from app.core import (
    MAX_ICON_BYTES,
    MAX_SOURCE_BYTES,
    fetch_play_store_metadata,
    parse_play_store_url,
    prepare_android_source,
)

ROOT = Path(__file__).resolve().parent
app = FastAPI(
    title="AppForge",
    description="أداة لتخصيص مشاريع Android التي تملك حق تعديلها.",
    version="0.1.0",
    docs_url="/api/docs",
    redoc_url=None,
)


class AnalyzeRequest(BaseModel):
    play_store_url: str = Field(min_length=10, max_length=2000)


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def home() -> str:
    return (ROOT / "index.html").read_text(encoding="utf-8")


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "name": "AppForge", "version": "0.1.0"}


@app.post("/api/analyze")
def analyze(payload: AnalyzeRequest):
    try:
        package_id = parse_play_store_url(payload.play_store_url)
        # Fetch only public listing metadata. The binary itself is never downloaded.
        result = fetch_play_store_metadata(payload.play_store_url)
        result["package_id"] = package_id
        result["source"] = "Google Play listing metadata"
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/prepare-source")
async def prepare_source(
    source_zip: Annotated[UploadFile, File(description="Android Gradle source project ZIP")],
    app_name: Annotated[str, Form(min_length=1, max_length=60)],
    package_id: Annotated[str, Form(min_length=3, max_length=200)],
    icon: Annotated[UploadFile | None, File(description="Optional PNG/JPEG launcher icon")] = None,
):
    data = await source_zip.read(MAX_SOURCE_BYTES + 1)
    await source_zip.close()
    if len(data) > MAX_SOURCE_BYTES:
        raise HTTPException(status_code=413, detail="حجم ZIP المضغوط يتجاوز 50 ميغابايت.")

    icon_data: bytes | None = None
    if icon is not None and icon.filename:
        icon_data = await icon.read(MAX_ICON_BYTES + 1)
        await icon.close()
        if len(icon_data) > MAX_ICON_BYTES:
            raise HTTPException(status_code=413, detail="حجم الأيقونة يتجاوز 4 ميغابايت.")

    try:
        output_path, work, warnings = prepare_android_source(data, app_name, package_id, icon_data)
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        # Do not expose filesystem paths or stack traces to the client.
        raise HTTPException(status_code=500, detail="تعذرت معالجة المشروع. تأكد من أن ZIP يحتوي مشروع Android Gradle صالحًا.") from exc

    warning_header = " | ".join(warnings)[:1800]
    return FileResponse(
        path=output_path,
        media_type="application/zip",
        filename="appforge-prepared-source.zip",
        headers={"X-AppForge-Warnings": warning_header},
        background=BackgroundTask(work.cleanup),
    )


@app.exception_handler(413)
async def too_large_handler(_, __):
    return JSONResponse(status_code=413, content={"detail": "حجم الملف أكبر من الحد المسموح."})
