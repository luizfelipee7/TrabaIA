from datetime import datetime
from time import perf_counter
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
import json

from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.ai.agent import InventoryAIAgent
from app.ai.llm_client import LocalLLMClient, LocalLLMError, MODEL_STATE
from app.ai.qa import (
    enrich_and_save_run,
    get_batch_export_path,
    get_run,
    get_run_export_path,
    list_batches,
    list_runs,
    save_batch_artifacts,
)
from app.ai.schemas import AIBatchReviewRequest, AIModelSelectRequest, AIReviewRequest
from app.ai.tools import list_ai_reports, read_ai_logs
from app.database import BASE_DIR, get_db


router = APIRouter(prefix="/ai", tags=["ai"])
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@router.get("/dashboard", response_class=HTMLResponse)
def ai_dashboard(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="ai_dashboard.html",
        context={
            "request": request,
            "base_url": MODEL_STATE.base_url,
            "selected_model": MODEL_STATE.selected_model,
        },
    )


@router.get("/models")
def list_models():
    try:
        client = LocalLLMClient()
        result = client.list_models()
        return result
    except LocalLLMError as exc:
        return {
            "ok": False,
            "base_url": MODEL_STATE.base_url,
            "selected_model": MODEL_STATE.selected_model,
            "models": MODEL_STATE.last_models,
            "message": str(exc),
        }


@router.post("/models/select")
def select_model(payload: AIModelSelectRequest):
    try:
        client = LocalLLMClient()
        response = client.set_model(payload.model_name)
    except LocalLLMError as exc:
        return {"ok": False, "message": str(exc)}

    response["ok"] = True
    response["load_attempt"] = None
    if payload.attempt_load:
        try:
            response["load_attempt"] = client.load_model(payload.model_name)
        except LocalLLMError as exc:
            response["load_attempt"] = {"ok": False, "message": str(exc)}
    return response


@router.post("/daily-inventory-review")
def run_daily_inventory_review(
    payload: AIReviewRequest | None = None,
    db: Session = Depends(get_db),
):
    try:
        client = LocalLLMClient()
    except LocalLLMError as exc:
        return {"status": "error", "message": str(exc), "steps": []}

    agent = InventoryAIAgent(db=db, llm_client=client)
    objective = payload.objective if payload else None
    return _run_review_with_qa(agent, objective=objective)


@router.post("/daily-inventory-review/stream")
def stream_daily_inventory_review(
    payload: AIReviewRequest | None = None,
    db: Session = Depends(get_db),
):
    try:
        client = LocalLLMClient()
    except LocalLLMError as exc:
        return StreamingResponse(
            _single_sse({"event": "run_error", "type": "error", "message": str(exc)}),
            media_type="text/event-stream",
        )

    agent = InventoryAIAgent(db=db, llm_client=client)
    objective = payload.objective if payload else None
    return StreamingResponse(
        _stream_review_events(agent, objective=objective),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/daily-inventory-review/batch")
def run_daily_inventory_review_batch(
    payload: AIBatchReviewRequest,
    db: Session = Depends(get_db),
):
    count = max(1, min(payload.count, 50))
    batch_id = f"batch-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:8]}"
    runs = []

    try:
        client = LocalLLMClient()
    except LocalLLMError as exc:
        return {"status": "error", "message": str(exc), "runs": []}

    for index in range(1, count + 1):
        agent = InventoryAIAgent(db=db, llm_client=client)
        run = _run_review_with_qa(
            agent,
            objective=payload.objective,
            batch_id=batch_id,
            batch_index=index,
        )
        runs.append(run)

    summary = save_batch_artifacts(batch_id, runs, objective=payload.objective)
    return {"status": "completed", "batch": summary, "runs": runs}


@router.get("/logs")
def get_ai_logs(limit: int = 50):
    return {"logs": read_ai_logs(limit=limit)}


@router.get("/reports")
def get_ai_reports(limit: int = 20):
    return {"reports": list_ai_reports(limit=limit)}


@router.get("/qa/runs")
def get_qa_runs(limit: int = 50):
    return {"runs": list_runs(limit=limit)}


@router.get("/qa/runs/{run_id}")
def get_qa_run(run_id: str):
    run = get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run de QA nao encontrado.")
    return run


@router.get("/qa/runs/{run_id}/export")
def export_qa_run(run_id: str):
    path = get_run_export_path(run_id)
    if not path:
        raise HTTPException(status_code=404, detail="Export do run nao encontrado.")
    return FileResponse(path, media_type="application/json", filename=path.name)


@router.get("/qa/batches")
def get_qa_batches(limit: int = 25):
    return {"batches": list_batches(limit=limit)}


@router.get("/qa/batches/{batch_id}/export")
def export_qa_batch(batch_id: str, format: str = "json"):
    export_format = "csv" if format == "csv" else "json"
    path = get_batch_export_path(batch_id, export_format)
    if not path:
        raise HTTPException(status_code=404, detail="Export do batch nao encontrado.")
    media_type = "text/csv" if export_format == "csv" else "application/json"
    return FileResponse(path, media_type=media_type, filename=path.name)


def _run_review_with_qa(
    agent: InventoryAIAgent,
    *,
    objective: str | None,
    batch_id: str | None = None,
    batch_index: int | None = None,
):
    started_at = datetime.utcnow().isoformat()
    start = perf_counter()
    result = agent.run_daily_inventory_review(objective=objective)
    duration_ms = int((perf_counter() - start) * 1000)
    finished_at = datetime.utcnow().isoformat()
    return enrich_and_save_run(
        result,
        objective=objective,
        model=MODEL_STATE.selected_model,
        started_at=started_at,
        finished_at=finished_at,
        duration_ms=duration_ms,
        batch_id=batch_id,
        batch_index=batch_index,
    )


def _stream_review_events(agent: InventoryAIAgent, *, objective: str | None):
    started_at = datetime.utcnow().isoformat()
    start = perf_counter()
    final_result = None
    for event in agent.stream_daily_inventory_review(objective=objective):
        if event.get("event") in {"run_completed", "run_error"} and isinstance(event.get("result"), dict):
            final_result = event["result"]
            duration_ms = int((perf_counter() - start) * 1000)
            finished_at = datetime.utcnow().isoformat()
            enriched = enrich_and_save_run(
                final_result,
                objective=objective,
                model=MODEL_STATE.selected_model,
                started_at=started_at,
                finished_at=finished_at,
                duration_ms=duration_ms,
            )
            event = {**event, "result": enriched}
        yield _format_sse(event)
    if final_result is None:
        yield _format_sse({"event": "stream_closed", "type": "system", "message": "Stream encerrado."})


def _single_sse(event: dict):
    yield _format_sse(event)


def _format_sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"
