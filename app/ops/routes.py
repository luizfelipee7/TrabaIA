from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
import json
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.ops import services as ops
from app.ai.llm_client import MODEL_STATE
from app.ai.model_policy import model_policy_snapshot
from app.ai.runtime_guard import current_ai_runtime
from app.database import BASE_DIR, get_db


router = APIRouter(tags=["operational"])
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


class SearchRequest(BaseModel):
    query: str = ""
    scope: str = "estoque"


class ReportRunRequest(BaseModel):
    report_type: str = "stock_check"
    use_ai: bool = False
    objective: str | None = None


class AgentRequest(BaseModel):
    prompt: str


class DocumentSaveRequest(BaseModel):
    supplier_name: str = ""
    date: str = ""
    amount: str = ""
    due_date: str = ""
    description: str = ""
    category: str = ""
    notes: str = ""
    file_path: str = ""


class MeetingSummaryRequest(BaseModel):
    text: str


@router.get("/assistente", response_class=HTMLResponse)
def operational_app(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="operational_app.html",
        context={
            "request": request,
            "selected_model": MODEL_STATE.selected_model,
            "base_url": MODEL_STATE.base_url,
        },
    )


@router.get("/ops/status")
def status():
    return {
        "model": ops.model_status(),
        "model_policy": model_policy_snapshot(),
        "ai_runtime": current_ai_runtime(),
        "stt": ops.stt_status(),
        "ocr": ops.ocr_status(),
    }


@router.get("/ops/dashboard")
def dashboard(db: Session = Depends(get_db)):
    return ops.project_dashboard(db)


@router.post("/ops/search")
def search(payload: SearchRequest, db: Session = Depends(get_db)):
    return ops.search_inventory(db, query=payload.query, scope=payload.scope)


@router.post("/ops/agent/request")
def agent_request(payload: AgentRequest, db: Session = Depends(get_db)):
    if not payload.prompt.strip():
        raise HTTPException(status_code=400, detail="Prompt vazio.")
    return ops.run_operational_request(db, payload.prompt.strip())


@router.post("/ops/agent/request/stream")
def agent_request_stream(payload: AgentRequest, db: Session = Depends(get_db)):
    if not payload.prompt.strip():
        raise HTTPException(status_code=400, detail="Prompt vazio.")
    return StreamingResponse(
        _stream_events(ops.stream_operational_request(db, payload.prompt.strip())),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/ops/reports/run")
def run_report(payload: ReportRunRequest, db: Session = Depends(get_db)):
    return ops.run_programmed_report(
        db,
        payload.report_type,
        use_ai=payload.use_ai,
        objective=payload.objective,
    )


@router.get("/ops/reports")
def reports(limit: int = 20):
    from app.ai.tools import list_ai_reports

    return {"reports": list_ai_reports(limit=limit)}


@router.get("/ops/reports/{filename}")
def report_file(filename: str):
    report = ops.read_report_file(filename)
    if report is None:
        raise HTTPException(status_code=404, detail="Relatorio nao encontrado.")
    return report


@router.get("/ops/history")
def history(limit: int = 20):
    from app.ai.qa import list_runs
    from app.ai.tools import read_ai_logs

    return {"runs": list_runs(limit=limit), "logs": read_ai_logs(limit=limit)}


@router.post("/ops/ocr/process")
async def process_ocr(file: UploadFile = File(...)):
    content = await file.read()
    return ops.process_ocr_upload(file.filename or "documento", content, file.content_type)


@router.post("/ops/ocr/save")
def save_ocr(payload: DocumentSaveRequest):
    return ops.save_document_record(payload.model_dump())


@router.get("/ops/ocr/documents")
def documents(limit: int = 50):
    return {"documents": ops.list_documents(limit=limit)}


@router.delete("/ops/ocr/documents/{document_id}")
def delete_document(document_id: str):
    result = ops.delete_document_record(document_id)
    if not result.get("deleted"):
        raise HTTPException(status_code=404, detail=result.get("message") or "Documento nao encontrado.")
    return result


@router.post("/ops/stt/transcribe")
async def stt_transcribe(file: UploadFile = File(...)):
    content = await file.read()
    return ops.transcribe_audio(file.filename or "audio.webm", content, file.content_type)


@router.get("/ops/stt/status")
def stt_status():
    return ops.stt_status()


@router.post("/v1/audio/transcriptions")
async def openai_compatible_audio_transcription(file: UploadFile = File(...)):
    content = await file.read()
    result = ops.transcribe_audio(file.filename or "audio.webm", content, file.content_type)
    transcription = result.get("transcription") or {}
    return {
        "text": transcription.get("text", ""),
        "language": transcription.get("language"),
        "segments": transcription.get("segments") or [],
        "status": result.get("status"),
        "engine": transcription.get("engine") or result.get("engine"),
        "message": result.get("message"),
    }


@router.post("/ops/meetings/summary")
def meeting_summary(payload: MeetingSummaryRequest):
    return ops.summarize_meeting(payload.model_dump())


@router.get("/ops/meetings")
def meetings(limit: int = 30):
    return {
        "meetings": ops.list_meetings(limit=limit),
        "memory": ops.list_meeting_memory(limit=limit),
    }


def _stream_events(events):
    for event in events:
        yield f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"
