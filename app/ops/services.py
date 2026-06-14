"""Service layer for the operational assistant UI."""

from __future__ import annotations

import base64
import json
import os
import unicodedata
import uuid
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from time import perf_counter
from typing import Any, Iterator

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import models
from app.ai.llm_client import LocalLLMClient, LocalLLMError, MODEL_STATE
from app.ai.model_policy import active_model_for_task, client_for_task, model_policy_snapshot
from app.ai.operational_agent import OperationalAIAgent
from app.ai.qa import list_runs
from app.ai.runtime_guard import AIRuntimeBusy, acquire_ai_runtime, current_ai_runtime
from app.ai.tools import list_ai_reports, read_ai_logs
from app.database import BASE_DIR
from app.integrations import stt as stt_integration


OPS_DIR = BASE_DIR / "operational_data"
UPLOADS_DIR = OPS_DIR / "uploads"
DOCUMENTS_FILE = OPS_DIR / "documents.json"
MEETINGS_FILE = OPS_DIR / "meetings.json"
MEETING_MEMORY_FILE = OPS_DIR / "meeting_memory.json"


def project_dashboard(db: Session) -> dict[str, Any]:
    today = date.today()
    products = db.scalars(select(models.Product).order_by(models.Product.name)).all()
    open_alerts = db.scalars(
        select(models.StockAlert)
        .where(models.StockAlert.status == "open")
        .order_by(models.StockAlert.created_at.desc())
    ).all()
    suppliers = db.scalars(select(models.Supplier).order_by(models.Supplier.name)).all()

    low_stock = [product for product in products if product.current_stock < product.minimum_stock]
    expiration_risks = [
        product
        for product in products
        if product.expiration_date and today <= product.expiration_date <= today + timedelta(days=30)
    ]
    abnormal_alerts = [alert for alert in open_alerts if alert.alert_type == "abnormal_consumption"]
    supplier_issues = [supplier for supplier in suppliers if not supplier.email or not supplier.phone]
    reports = list_ai_reports(limit=8)
    runs = list_runs(limit=8)
    documents = list_documents(limit=8)
    meetings = list_meetings(limit=8)
    meeting_memory = list_meeting_memory(limit=8)

    return {
        "generated_at": datetime.utcnow().isoformat(),
        "model_status": model_status(),
        "model_policy": model_policy_snapshot(),
        "stt_status": stt_status(),
        "ocr_status": ocr_status(),
        "metrics": {
            "products": len(products),
            "open_alerts": len(open_alerts),
            "low_stock": len(low_stock),
            "expiration_risks": len(expiration_risks),
            "abnormal_consumption": len(abnormal_alerts),
            "supplier_issues": len(supplier_issues),
            "reports": len(reports),
            "runs": len(runs),
            "documents": len(documents),
            "meetings": len(meetings),
            "meeting_memory_notes": len(meeting_memory),
            "pending_actions": _pending_action_count(reports),
        },
        "low_stock": [_product_row(product) for product in low_stock[:10]],
        "expiration_risks": [_product_row(product) for product in expiration_risks[:10]],
        "abnormal_consumption": [_alert_row(alert) for alert in abnormal_alerts[:10]],
        "supplier_issues": [_supplier_row(supplier) for supplier in supplier_issues[:10]],
        "reports": reports,
        "runs": runs,
        "documents": documents,
        "meetings": meetings,
        "meeting_memory": meeting_memory,
        "logs": read_ai_logs(limit=8),
    }


def search_inventory(db: Session, query: str = "", scope: str = "estoque") -> dict[str, Any]:
    normalized = " ".join((query or "").lower().split())
    today = date.today()
    products = db.scalars(select(models.Product).order_by(models.Product.name)).all()
    open_alerts = db.scalars(
        select(models.StockAlert)
        .where(models.StockAlert.status == "open")
        .order_by(models.StockAlert.created_at.desc())
    ).all()
    suppliers = db.scalars(select(models.Supplier).order_by(models.Supplier.name)).all()

    if _looks_like_low_stock(normalized, scope):
        rows = [product for product in products if product.current_stock < product.minimum_stock]
        rows.sort(key=lambda product: (product.current_stock - product.minimum_stock, product.criticality))
        intent = "low_stock"
        results = [_product_row(product) for product in rows]
    elif _looks_like_expiration(normalized, scope):
        rows = [
            product
            for product in products
            if product.expiration_date and today <= product.expiration_date <= today + timedelta(days=30)
        ]
        rows.sort(key=lambda product: product.expiration_date or today)
        intent = "expiration_risks"
        results = [_product_row(product) for product in rows]
    elif _looks_like_abnormal(normalized, scope):
        rows = [alert for alert in open_alerts if alert.alert_type == "abnormal_consumption"]
        intent = "abnormal_consumption"
        results = [_alert_row(alert) for alert in rows]
    elif _looks_like_supplier(normalized, scope):
        rows = [supplier for supplier in suppliers if not supplier.email or not supplier.phone]
        intent = "supplier_issues"
        results = [_supplier_row(supplier) for supplier in rows]
    elif "relatorio" in normalized or scope == "relatorios":
        intent = "reports"
        results = list_ai_reports(limit=20)
    else:
        intent = "free_product_search"
        terms = [term for term in normalized.split() if term]
        rows = []
        for product in products:
            haystack = f"{product.name} {product.sku} {product.category}".lower()
            if not terms or all(term in haystack for term in terms):
                rows.append(product)
        results = [_product_row(product) for product in rows[:50]]

    return {
        "query": query,
        "scope": scope,
        "intent": intent,
        "count": len(results),
        "results": results,
        "controlled_by_code": True,
    }


def run_operational_request(db: Session, prompt: str) -> dict[str, Any]:
    started = perf_counter()
    query = " ".join((prompt or "").strip().split())
    normalized = query.lower()
    if not query:
        return _delivery(
            title="Pedido vazio",
            summary="Nenhuma operacao executada.",
            rows=[],
            columns=[],
            mode="invalid_request",
            duration_ms=_elapsed_ms(started),
        )

    if _looks_like_ai_review_request(normalized):
        result = run_programmed_report(db, "daily_inventory_review", use_ai=True, objective=query)
        return _delivery_from_report(result, duration_ms=_elapsed_ms(started))

    try:
        model = active_model_for_task("agent_request")
        with acquire_ai_runtime("operational_agent", model=model, detail=query):
            client = client_for_task("agent_request")
            result = OperationalAIAgent(db=db, llm_client=client).run(query)
        result["metadata"] = {
            **result.get("metadata", {}),
            "duration_ms": _elapsed_ms(started),
        }
        return result
    except AIRuntimeBusy as exc:
        return {
            "status": "ai_busy",
            "mode": "ai_answer",
            "source": "lm_studio",
            "title": "IA ocupada",
            "summary": str(exc),
            "visualization": {"type": "notice", "level": "warning"},
            "metadata": {"duration_ms": _elapsed_ms(started), "llm_calls": 0, "active_runtime": current_ai_runtime()},
        }
    except LocalLLMError as exc:
        return {
            "status": "model_unavailable",
            "mode": "ai_answer",
            "source": "lm_studio",
            "title": "IA indisponivel",
            "summary": str(exc),
            "visualization": {"type": "notice", "level": "danger"},
            "metadata": {"duration_ms": _elapsed_ms(started), "llm_calls": 0, "ai_error": str(exc)},
        }
    except Exception as exc:
        return {
            "status": "ai_response_invalid",
            "mode": "ai_answer",
            "source": "lm_studio",
            "title": "Resposta da IA invalida",
            "summary": f"O modelo respondeu, mas a aplicacao nao conseguiu interpretar a entrega: {type(exc).__name__}: {exc}",
            "visualization": {"type": "notice", "level": "danger"},
            "metadata": {"duration_ms": _elapsed_ms(started), "llm_calls": 1, "ai_error": f"{type(exc).__name__}: {exc}"},
        }


def stream_operational_request(db: Session, prompt: str) -> Iterator[dict[str, Any]]:
    started = perf_counter()
    query = " ".join((prompt or "").strip().split())
    if not query:
        yield {
            "event": "run_error",
            "type": "error",
            "message": "Prompt vazio.",
            "result": {
                "status": "error",
                "title": "Pedido vazio",
                "summary": "Digite ou grave uma solicitacao antes de enviar.",
                "visualization": {"type": "answer", "columns": [], "rows": [], "items": []},
                "metadata": {"duration_ms": _elapsed_ms(started), "llm_calls": 0, "tool_call_count": 0},
            },
        }
        return

    normalized = query.lower()
    if _looks_like_ai_review_request(normalized):
        yield {"event": "route_selected", "type": "system", "message": "Pedido encaminhado para revisao diaria de estoque."}
        result = run_programmed_report(db, "daily_inventory_review", use_ai=True, objective=query)
        delivery = _delivery_from_report(result, duration_ms=_elapsed_ms(started))
        yield {"event": "run_completed", "type": "response", "message": "Revisao diaria concluida.", "result": delivery}
        return

    try:
        model = active_model_for_task("agent_request")
        with acquire_ai_runtime("operational_agent", model=model, detail=query):
            client = client_for_task("agent_request")
            agent = OperationalAIAgent(db=db, llm_client=client)
            for event in agent.stream(query):
                if event.get("event") in {"run_completed", "run_error"} and isinstance(event.get("result"), dict):
                    event["result"]["metadata"] = {
                        **event["result"].get("metadata", {}),
                        "duration_ms": _elapsed_ms(started),
                    }
                yield event
    except AIRuntimeBusy as exc:
        yield {
            "event": "run_busy",
            "type": "warning",
            "message": str(exc),
            "result": {
                "status": "ai_busy",
                "mode": "operational_agent",
                "source": "lm_studio_tools",
                "title": "IA ocupada",
                "summary": str(exc),
                "visualization": {"type": "answer", "columns": [], "rows": [], "items": []},
                "metadata": {"duration_ms": _elapsed_ms(started), "llm_calls": 0, "tool_call_count": 0},
            },
        }
    except LocalLLMError as exc:
        yield {
            "event": "run_error",
            "type": "error",
            "message": str(exc),
            "result": {
                "status": "model_unavailable",
                "mode": "operational_agent",
                "source": "lm_studio_tools",
                "title": "IA indisponivel",
                "summary": str(exc),
                "visualization": {"type": "answer", "columns": [], "rows": [], "items": []},
                "metadata": {"duration_ms": _elapsed_ms(started), "llm_calls": 0, "tool_call_count": 0},
            },
        }


def run_programmed_report(db: Session, report_type: str, use_ai: bool = False, objective: str | None = None) -> dict[str, Any]:
    if report_type in {"stock_check", "deterministic_stock_check"}:
        from app import services

        return {
            "status": "completed",
            "report_type": "deterministic_stock_check",
            "result": services.run_stock_check(db),
            "message": "Checagem deterministica executada.",
        }

    if report_type in {"daily_inventory_review", "ai_daily_review"}:
        status = model_status("daily_inventory_review")
        if not status["available"]:
            return {
                "status": "model_unavailable",
                "report_type": "daily_inventory_review",
                "message": status["message"],
            }
        from app.ai.agent import InventoryAIAgent

        try:
            model = active_model_for_task("daily_inventory_review")
            with acquire_ai_runtime("daily_inventory_review", model=model, detail=objective or ""):
                client = client_for_task("daily_inventory_review")
                result = InventoryAIAgent(db=db, llm_client=client).run_daily_inventory_review(objective=objective)
        except AIRuntimeBusy as exc:
            return {"status": "ai_busy", "report_type": report_type, "message": str(exc), "active_runtime": current_ai_runtime()}
        except LocalLLMError as exc:
            return {"status": "model_unavailable", "report_type": report_type, "message": str(exc)}
        return {"status": result.get("status"), "report_type": report_type, "result": result}

    if use_ai:
        return run_programmed_report(db, "daily_inventory_review", objective=objective)

    return {
        "status": "unsupported_report",
        "report_type": report_type,
        "message": "Rotina nao cadastrada. Use stock_check ou daily_inventory_review.",
    }


def list_documents(limit: int = 50) -> list[dict[str, Any]]:
    documents = _read_json_list(DOCUMENTS_FILE)
    return sorted(documents, key=lambda item: item.get("created_at", ""), reverse=True)[:limit]


def list_meetings(limit: int = 50) -> list[dict[str, Any]]:
    meetings = _read_json_list(MEETINGS_FILE)
    return sorted(meetings, key=lambda item: item.get("created_at", ""), reverse=True)[:limit]


def list_meeting_memory(limit: int = 50) -> list[dict[str, Any]]:
    memory = _read_json_list(MEETING_MEMORY_FILE)
    return sorted(memory, key=lambda item: item.get("updated_at", item.get("created_at", "")), reverse=True)[:limit]


def process_ocr_upload(filename: str, content: bytes, content_type: str | None) -> dict[str, Any]:
    saved = _save_upload(filename, content, "documents")
    model = model_status("ocr")
    base_payload = {
        "status": "model_unavailable" if not model["available"] else "pending_model_processing",
        "message": model["message"] if not model["available"] else "Arquivo salvo. Tentando extracao por modelo local.",
        "file": saved,
        "extracted": _empty_document_fields(),
    }

    if not model["available"]:
        return base_payload

    if not (content_type or "").startswith("image/"):
        return {
            **base_payload,
            "status": "unsupported_media",
            "message": "Arquivo salvo. OCR via modelo local esta preparado para imagens; PDF fica pendente de conversao/OCR externo.",
        }

    try:
        extracted = _extract_document_with_model(content, content_type or "image/png")
    except LocalLLMError as exc:
        return {
            **base_payload,
            "status": "model_unavailable",
            "message": str(exc),
        }
    except Exception as exc:
        return {
            **base_payload,
            "status": "ocr_failed",
            "message": f"O modelo nao retornou OCR valido: {type(exc).__name__}: {exc}",
        }

    return {
        "status": "completed",
        "message": "OCR extraido pelo modelo local. Confira antes de salvar.",
        "file": saved,
        "extracted": extracted,
    }


def save_document_record(payload: dict[str, Any]) -> dict[str, Any]:
    documents = _read_json_list(DOCUMENTS_FILE)
    record = {
        "id": f"doc-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}",
        "created_at": datetime.utcnow().isoformat(),
        "supplier_name": payload.get("supplier_name") or "",
        "date": payload.get("date") or "",
        "amount": payload.get("amount") or "",
        "due_date": payload.get("due_date") or "",
        "description": payload.get("description") or "",
        "category": payload.get("category") or "",
        "notes": payload.get("notes") or "",
        "file_path": payload.get("file_path") or "",
        "status": "saved_for_review",
    }
    documents.append(record)
    _write_json_list(DOCUMENTS_FILE, documents)
    return {"saved": True, "document": record}


def summarize_meeting(payload: dict[str, Any]) -> dict[str, Any]:
    started = perf_counter()
    text = (payload.get("text") or "").strip()
    if not text:
        return {"status": "invalid_request", "message": "Envie uma transcricao ou pauta em texto."}

    prepared = _prepare_meeting_text(text)
    relevant_context = _find_relevant_meeting_context(prepared["clean_text"], limit=5)
    pipeline_events: list[dict[str, Any]] = [
        {
            "event": "raw_text_received",
            "type": "input",
            "message": "Texto cru recebido para analise.",
            "chars": len(text),
        },
        {
            "event": "text_prepared",
            "type": "system",
            "message": "Texto normalizado para reduzir ruido antes da IA.",
            "chars": len(prepared["clean_text"]),
            "keywords": prepared["keywords"][:12],
        },
        {
            "event": "meeting_memory_search",
            "type": "memory",
            "message": f"{len(relevant_context)} registro(s) relevante(s) recuperado(s) da memoria local.",
            "context_ids": [item.get("id") for item in relevant_context],
        },
    ]

    status = model_status("meeting_summary")
    if not status["available"]:
        fallback_report = _fallback_meeting_report(prepared, relevant_context)
        record = _save_meeting_summary(
            {
                "status": "model_unavailable",
                "source_text": text,
                "clean_text": prepared["clean_text"],
                "keywords": prepared["keywords"],
                "relevant_context": relevant_context,
                "pipeline_events": pipeline_events,
                **fallback_report,
                "message": status["message"],
            }
        )
        memory_items = _append_meeting_memory(record, status="pending_model")
        pipeline_events.append(
            {
                "event": "meeting_memory_updated",
                "type": "memory",
                "message": "Registro pendente salvo na memoria operacional.",
                "memory_ids": [item.get("id") for item in memory_items],
            }
        )
        record["pipeline_events"] = pipeline_events
        _replace_meeting_summary(record)
        return {
            "status": "model_unavailable",
            "message": status["message"],
            "record": record,
            "pipeline_events": pipeline_events,
            "relevant_context": relevant_context,
            "memory_updates": memory_items,
            "duration_ms": _elapsed_ms(started),
        }

    try:
        model = active_model_for_task("meeting_summary")
        with acquire_ai_runtime("meeting_summary", model=model):
            client = client_for_task("meeting_summary")
            parsed = _generate_meeting_report_with_model(
                client=client,
                clean_text=prepared["clean_text"],
                relevant_context=relevant_context,
            )
        pipeline_events.append(
            {
                "event": "structured_meeting_report",
                "type": "model",
                "message": "IA gerou entrega estruturada da reuniao.",
                "model": model,
            }
        )
    except AIRuntimeBusy as exc:
        return {"status": "ai_busy", "message": str(exc), "active_runtime": current_ai_runtime()}
    except Exception as exc:
        fallback_report = _fallback_meeting_report(prepared, relevant_context)
        record = _save_meeting_summary(
            {
                "status": "summary_failed",
                "source_text": text,
                "clean_text": prepared["clean_text"],
                "keywords": prepared["keywords"],
                "relevant_context": relevant_context,
                "pipeline_events": pipeline_events,
                **fallback_report,
                "message": f"Falha ao resumir: {type(exc).__name__}: {exc}",
            }
        )
        memory_items = _append_meeting_memory(record, status="summary_failed")
        pipeline_events.append(
            {
                "event": "meeting_memory_updated",
                "type": "memory",
                "message": "Registro de falha salvo na memoria operacional.",
                "memory_ids": [item.get("id") for item in memory_items],
            }
        )
        record["pipeline_events"] = pipeline_events
        _replace_meeting_summary(record)
        return {
            "status": "summary_failed",
            "record": record,
            "pipeline_events": pipeline_events,
            "relevant_context": relevant_context,
            "memory_updates": memory_items,
            "duration_ms": _elapsed_ms(started),
        }

    normalized_report = _normalize_meeting_report(parsed, prepared, relevant_context)
    record = _save_meeting_summary(
        {
            "status": "completed",
            "source_text": text,
            "clean_text": prepared["clean_text"],
            "keywords": prepared["keywords"],
            "relevant_context": relevant_context,
            "pipeline_events": pipeline_events,
            **normalized_report,
        }
    )
    memory_items = _append_meeting_memory(record, status="active")
    pipeline_events.append(
        {
            "event": "meeting_memory_updated",
            "type": "memory",
            "message": f"{len(memory_items)} anotacao(oes) atualizada(s) na memoria operacional.",
            "memory_ids": [item.get("id") for item in memory_items],
        }
    )
    record["pipeline_events"] = pipeline_events
    _replace_meeting_summary(record)
    return {
        "status": "completed",
        "message": "Reuniao analisada, conectada a memoria local e salva.",
        "record": record,
        "pipeline_events": pipeline_events,
        "relevant_context": relevant_context,
        "memory_updates": memory_items,
        "duration_ms": _elapsed_ms(started),
    }


def _prepare_meeting_text(text: str) -> dict[str, Any]:
    lines = []
    for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = " ".join(raw_line.strip().split())
        if line:
            lines.append(line)
    clean_text = "\n".join(lines)
    return {
        "clean_text": clean_text[:16000],
        "keywords": _meeting_keywords(clean_text),
    }


def _meeting_keywords(text: str, *, limit: int = 35) -> list[str]:
    stop_words = {
        "para",
        "como",
        "com",
        "uma",
        "das",
        "dos",
        "que",
        "por",
        "foi",
        "ser",
        "ter",
        "vai",
        "sao",
        "reuniao",
        "sobre",
        "entre",
        "aqui",
        "mais",
        "isso",
        "esse",
        "essa",
        "esta",
        "este",
        "pela",
        "pelo",
        "ate",
    }
    counts: dict[str, int] = {}
    for token in _normalize_text_for_lookup(text).split():
        if len(token) < 4 or token in stop_words:
            continue
        counts[token] = counts.get(token, 0) + 1
    return [
        token
        for token, _count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]
    ]


def _find_relevant_meeting_context(clean_text: str, *, limit: int = 5) -> list[dict[str, Any]]:
    keywords = set(_meeting_keywords(clean_text, limit=40))
    if not keywords:
        return []

    candidates: list[tuple[int, str, dict[str, Any]]] = []
    for meeting in _read_json_list(MEETINGS_FILE):
        score = _score_context_text(meeting, keywords)
        if score > 0:
            candidates.append((score, meeting.get("created_at", ""), _meeting_context_row(meeting, score, "meeting")))

    for note in _read_json_list(MEETING_MEMORY_FILE):
        score = _score_context_text(note, keywords)
        if score > 0:
            candidates.append((score, note.get("updated_at", note.get("created_at", "")), _memory_context_row(note, score)))

    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for _score, _created_at, row in candidates:
        key = str(row.get("id") or row.get("title") or row.get("summary"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
        if len(deduped) >= limit:
            break
    return deduped


def _score_context_text(item: dict[str, Any], keywords: set[str]) -> int:
    payload = json.dumps(
        {
            "summary": item.get("summary"),
            "title": item.get("title"),
            "decisions": item.get("decisions"),
            "risks": item.get("risks"),
            "next_actions": item.get("next_actions"),
            "topics": item.get("topics"),
            "entities": item.get("entities"),
            "watch_items": item.get("watch_items"),
            "commitments": item.get("commitments"),
        },
        ensure_ascii=False,
        default=str,
    )
    haystack = set(_normalize_text_for_lookup(payload).split())
    return len(keywords.intersection(haystack))


def _meeting_context_row(meeting: dict[str, Any], score: int, source: str) -> dict[str, Any]:
    return {
        "id": meeting.get("id"),
        "source": source,
        "score": score,
        "created_at": meeting.get("created_at"),
        "summary": meeting.get("summary") or meeting.get("title") or "",
        "decisions": _as_list(meeting.get("decisions"))[:5],
        "next_actions": _as_list(meeting.get("next_actions"))[:5],
    }


def _memory_context_row(note: dict[str, Any], score: int) -> dict[str, Any]:
    return {
        "id": note.get("id"),
        "source": "meeting_memory",
        "score": score,
        "created_at": note.get("updated_at") or note.get("created_at"),
        "summary": note.get("summary") or note.get("topic") or "",
        "decisions": _as_list(note.get("commitments"))[:5],
        "next_actions": _as_list(note.get("watch_items"))[:5],
    }


def _generate_meeting_report_with_model(
    *,
    client: LocalLLMClient,
    clean_text: str,
    relevant_context: list[dict[str, Any]],
) -> dict[str, Any]:
    messages = _meeting_messages(clean_text, relevant_context)
    response_format = _meeting_response_format()
    try:
        response = client.chat_completion(messages, response_format=response_format)
        content = response.choices[0].message.content or "{}"
    except LocalLLMError as exc:
        if "response_format" not in str(exc) and "json_schema" not in str(exc):
            raise
        content = client.chat(
            [
                *messages,
                {
                    "role": "user",
                    "content": (
                        "O endpoint nao aceitou schema nativo. Responda novamente somente com JSON valido "
                        "e exatamente com os campos solicitados, sem markdown."
                    ),
                },
            ]
        )
    return json.loads(_extract_json(content))


def _meeting_messages(clean_text: str, relevant_context: list[dict[str, Any]]) -> list[dict[str, str]]:
    context = json.dumps(relevant_context, ensure_ascii=False, default=str)
    return [
        {
            "role": "system",
            "content": (
                "Voce e um analista operacional. Transforme texto cru de reuniao em uma entrega de trabalho. "
                "Nao faca resumo generico: identifique decisoes, riscos, acoes, pendencias e aprendizados para memoria. "
                "Use o contexto de reunioes anteriores apenas quando ajudar. Nao invente fatos ausentes. "
                "A saida deve seguir exatamente o JSON Schema recebido."
            ),
        },
        {
            "role": "user",
            "content": (
                "Contexto relevante recuperado da memoria local:\n"
                f"{context}\n\n"
                "Texto cru normalizado da reuniao atual:\n"
                f"{clean_text[:16000]}"
            ),
        },
    ]


def _meeting_response_format() -> dict[str, Any]:
    schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "summary": {"type": "string"},
            "cleaned_meeting": {"type": "string"},
            "relevant_context_used": {
                "type": "array",
                "items": {"type": "string"},
            },
            "insights": {"type": "array", "items": {"type": "string"}},
            "decisions": {"type": "array", "items": {"type": "string"}},
            "risks": {"type": "array", "items": {"type": "string"}},
            "next_actions": {"type": "array", "items": {"type": "string"}},
            "open_questions": {"type": "array", "items": {"type": "string"}},
            "memory_updates": {
                "type": "object",
                "properties": {
                    "entities": {"type": "array", "items": {"type": "string"}},
                    "topics": {"type": "array", "items": {"type": "string"}},
                    "commitments": {"type": "array", "items": {"type": "string"}},
                    "watch_items": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["entities", "topics", "commitments", "watch_items"],
                "additionalProperties": False,
            },
            "markdown_report": {"type": "string"},
            "mermaid_diagram": {"type": "string"},
        },
        "required": [
            "title",
            "summary",
            "cleaned_meeting",
            "relevant_context_used",
            "insights",
            "decisions",
            "risks",
            "next_actions",
            "open_questions",
            "memory_updates",
            "markdown_report",
            "mermaid_diagram",
        ],
        "additionalProperties": False,
    }
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "MeetingOperationalReport",
            "strict": True,
            "schema": schema,
        },
    }


def _normalize_meeting_report(
    parsed: dict[str, Any],
    prepared: dict[str, Any],
    relevant_context: list[dict[str, Any]],
) -> dict[str, Any]:
    memory_updates = parsed.get("memory_updates") if isinstance(parsed.get("memory_updates"), dict) else {}
    report = {
        "title": str(parsed.get("title") or "Reuniao operacional"),
        "summary": str(parsed.get("summary") or ""),
        "cleaned_meeting": str(parsed.get("cleaned_meeting") or prepared["clean_text"]),
        "relevant_context_used": _as_list(parsed.get("relevant_context_used")),
        "insights": _as_list(parsed.get("insights")),
        "decisions": _as_list(parsed.get("decisions")),
        "risks": _as_list(parsed.get("risks")),
        "next_actions": _as_list(parsed.get("next_actions")),
        "open_questions": _as_list(parsed.get("open_questions")),
        "memory_updates": {
            "entities": _as_list(memory_updates.get("entities")),
            "topics": _as_list(memory_updates.get("topics")),
            "commitments": _as_list(memory_updates.get("commitments")),
            "watch_items": _as_list(memory_updates.get("watch_items")),
        },
        "markdown_report": str(parsed.get("markdown_report") or ""),
        "mermaid_diagram": str(parsed.get("mermaid_diagram") or ""),
    }
    if not report["relevant_context_used"] and relevant_context:
        report["relevant_context_used"] = [str(item.get("id")) for item in relevant_context if item.get("id")]
    if not report["markdown_report"]:
        report["markdown_report"] = _meeting_markdown(report)
    if not report["mermaid_diagram"]:
        report["mermaid_diagram"] = _meeting_mermaid(report)
    return report


def _fallback_meeting_report(prepared: dict[str, Any], relevant_context: list[dict[str, Any]]) -> dict[str, Any]:
    title = "Reuniao pendente de analise por IA"
    summary = "Texto salvo e contexto local recuperado. A analise estruturada fica pendente ate o modelo correto estar disponivel."
    report = {
        "title": title,
        "summary": summary,
        "cleaned_meeting": prepared["clean_text"],
        "relevant_context_used": [str(item.get("id")) for item in relevant_context if item.get("id")],
        "insights": [],
        "decisions": [],
        "risks": [],
        "next_actions": [],
        "open_questions": [],
        "memory_updates": {
            "entities": [],
            "topics": prepared["keywords"][:10],
            "commitments": [],
            "watch_items": [],
        },
    }
    return {**report, "markdown_report": _meeting_markdown(report), "mermaid_diagram": _meeting_mermaid(report)}


def _append_meeting_memory(record: dict[str, Any], *, status: str) -> list[dict[str, Any]]:
    updates = record.get("memory_updates") if isinstance(record.get("memory_updates"), dict) else {}
    topics = _as_list(updates.get("topics")) or _as_list(record.get("keywords"))[:8]
    commitments = _as_list(updates.get("commitments"))
    watch_items = _as_list(updates.get("watch_items"))
    entities = _as_list(updates.get("entities"))

    note = {
        "id": f"mem-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}",
        "meeting_id": record.get("id"),
        "created_at": datetime.utcnow().isoformat(),
        "updated_at": datetime.utcnow().isoformat(),
        "status": status,
        "title": record.get("title") or "Reuniao",
        "summary": record.get("summary") or "",
        "topics": topics,
        "entities": entities,
        "commitments": commitments,
        "watch_items": watch_items,
    }
    memory = _read_json_list(MEETING_MEMORY_FILE)
    memory.append(note)
    memory = sorted(memory, key=lambda item: item.get("updated_at", ""), reverse=True)[:250]
    _write_json_list(MEETING_MEMORY_FILE, memory)
    return [note]


def _meeting_markdown(report: dict[str, Any]) -> str:
    def bullets(values: Any) -> str:
        rows = _as_list(values)
        return "\n".join(f"- {value}" for value in rows) if rows else "- Nenhum item identificado."

    return (
        f"# {report.get('title') or 'Reuniao operacional'}\n\n"
        f"**Resumo:** {report.get('summary') or 'Sem resumo.'}\n\n"
        "## Insights\n"
        f"{bullets(report.get('insights'))}\n\n"
        "## Decisoes\n"
        f"{bullets(report.get('decisions'))}\n\n"
        "## Riscos\n"
        f"{bullets(report.get('risks'))}\n\n"
        "## Proximas acoes\n"
        f"{bullets(report.get('next_actions'))}\n"
    )


def _meeting_mermaid(report: dict[str, Any]) -> str:
    decisions = _as_list(report.get("decisions"))[:3]
    actions = _as_list(report.get("next_actions"))[:4]
    lines = ['graph TD', '  Start["Reuniao analisada"]']
    for index, decision in enumerate(decisions):
        lines.append(f'  Start --> Dec{index}["Decisao: {_mermaid_label(decision)}"]')
    for index, action in enumerate(actions):
        lines.append(f'  Start --> Act{index}["Acao: {_mermaid_label(action)}"]')
    if len(lines) == 2:
        lines.append('  Start --> Note["Sem acoes estruturadas"]')
    return "\n".join(lines)


def _mermaid_label(value: str) -> str:
    return str(value).replace('"', "'").replace("\n", " ")[:90]


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, tuple):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    return [text] if text else []


def _normalize_text_for_lookup(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    normalized = []
    for char in text.lower():
        if unicodedata.combining(char):
            continue
        normalized.append(char if char.isalnum() else " ")
    return " ".join("".join(normalized).split())


def stt_status() -> dict[str, Any]:
    return stt_integration.status()


def transcribe_audio(filename: str, content: bytes, content_type: str | None) -> dict[str, Any]:
    saved = _save_upload(filename, content, "audio")
    result = stt_integration.transcribe(filename, content, content_type)
    if result.get("status") == "completed":
        return {"status": "completed", "file": saved, "transcription": result}
    return {**result, "file": saved}


def model_status(task: str = "agent_request") -> dict[str, Any]:
    url = f"{MODEL_STATE.base_url.rstrip('/')}/models"
    try:
        with urllib.request.urlopen(url, timeout=1.5) as response:
            data = json.loads(response.read().decode("utf-8"))
            models = data.get("data", []) if isinstance(data, dict) else []
            model_ids = [str(model.get("id") or "") for model in models if isinstance(model, dict) and model.get("id")]
            generation_model_ids = [model_id for model_id in model_ids if "embed" not in model_id.lower()]
            expected_model = active_model_for_task(task)
            policy_ready = bool(model_ids) and expected_model in model_ids
            single_generation_model = len(generation_model_ids) <= 1
            if policy_ready and not single_generation_model:
                message = (
                    f"Modelo esperado disponivel ({expected_model}), mas o LM Studio esta com "
                    f"{len(generation_model_ids)} modelos generativos carregados. O app nao carrega modelos "
                    "automaticamente; descarregue os extras no LM Studio para cumprir a regra de um modelo por vez."
                )
            elif policy_ready:
                message = "Modelo local alinhado com a politica operacional."
            else:
                message = f"LM Studio acessivel, mas o modelo esperado para {task} e {expected_model}."
            return {
                "available": policy_ready,
                "base_url": MODEL_STATE.base_url,
                "selected_model": MODEL_STATE.selected_model,
                "expected_model": expected_model,
                "task": task,
                "available_models": model_ids,
                "generation_models": generation_model_ids,
                "policy_ready": policy_ready,
                "single_generation_model": single_generation_model,
                "policy": model_policy_snapshot(),
                "runtime": current_ai_runtime(),
                "model_count": len(models),
                "message": message,
            }
    except Exception as exc:
        return {
            "available": False,
            "base_url": MODEL_STATE.base_url,
            "selected_model": MODEL_STATE.selected_model,
            "policy": model_policy_snapshot(),
            "runtime": current_ai_runtime(),
            "model_count": 0,
            "message": f"Modelo indisponivel: {type(exc).__name__}.",
        }


def ocr_status() -> dict[str, Any]:
    status = model_status("ocr")
    return {
        "available": status["available"],
        "message": "OCR via modelo local disponivel." if status["available"] else "OCR aguardando modelo local.",
        "model": status,
    }


def read_report_file(filename: str) -> dict[str, Any] | None:
    safe_name = Path(filename).name
    path = BASE_DIR / "reports" / safe_name
    if not path.exists() or path.suffix.lower() != ".json":
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _delivery(
    *,
    title: str,
    summary: str,
    rows: list[dict[str, Any]],
    columns: list[dict[str, str]],
    mode: str,
    source: str = "system",
    metadata: dict[str, Any] | None = None,
    duration_ms: int = 0,
) -> dict[str, Any]:
    return {
        "status": "completed",
        "mode": mode,
        "source": source,
        "title": title,
        "summary": summary,
        "visualization": {
            "type": "table",
            "columns": columns,
            "rows": rows,
        },
        "metadata": {
            "duration_ms": duration_ms,
            **(metadata or {}),
        },
    }


def _delivery_from_report(result: dict[str, Any], *, duration_ms: int) -> dict[str, Any]:
    if result.get("status") == "ai_busy":
        return {
            "status": "ai_busy",
            "mode": "ai_report",
            "source": "lm_studio",
            "title": "IA ocupada",
            "summary": result.get("message") or "Ja existe uma execucao de IA em andamento.",
            "visualization": {"type": "notice", "level": "warning", "rows": []},
            "metadata": {"duration_ms": duration_ms, "llm_calls": 0, "active_runtime": result.get("active_runtime")},
        }

    report = result.get("result", {}).get("final_report") or result.get("result", {}).get("report")
    if not report:
        return {
            "status": result.get("status") or "completed",
            "mode": "ai_report",
            "source": "lm_studio",
            "title": "Relatorio de IA",
            "summary": result.get("message") or "Resultado recebido.",
            "visualization": {"type": "raw", "payload": result},
            "metadata": {"duration_ms": duration_ms, "llm_calls": "ai_report"},
        }

    return {
        "status": result.get("status") or "completed",
        "mode": "ai_report",
        "source": "lm_studio",
        "title": "Revisao diaria de estoque",
        "summary": report.get("executive_summary") or "Relatorio gerado.",
        "visualization": {
            "type": "report",
            "report": report,
        },
        "metadata": {"duration_ms": duration_ms, "llm_calls": "ai_report"},
    }


def _looks_like_ai_review_request(query: str) -> bool:
    normalized = _normalize_text_for_lookup(query)
    markers = ("revisao diaria", "daily inventory review", "daily_inventory_review", "rodar ia", "executar ia")
    return any(marker in normalized for marker in markers)


def _elapsed_ms(started: float) -> int:
    return int((perf_counter() - started) * 1000)


def _save_upload(filename: str, content: bytes, folder: str) -> dict[str, Any]:
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    target_dir = UPLOADS_DIR / folder
    target_dir.mkdir(parents=True, exist_ok=True)
    safe_name = "".join(char if char.isalnum() or char in {".", "-", "_"} else "-" for char in Path(filename).name)
    target = target_dir / f"{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}-{safe_name}"
    target.write_bytes(content)
    return {"filename": filename, "path": str(target), "size": len(content)}


def _extract_document_with_model(content: bytes, content_type: str) -> dict[str, Any]:
    model = active_model_for_task("ocr")
    with acquire_ai_runtime("ocr", model=model):
        client = client_for_task("ocr")
        image_url = f"data:{content_type};base64,{base64.b64encode(content).decode('ascii')}"
        response = client.chat_completion(
            [
                {
                    "role": "system",
                    "content": (
                        "Extraia dados de conta/documento. Responda somente JSON com supplier_name, date, "
                        "amount, due_date, description, category e notes. Use string vazia quando incerto."
                    ),
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Leia o documento e extraia os campos operacionais basicos."},
                        {"type": "image_url", "image_url": {"url": image_url}},
                    ],
                },
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "DocumentOcrExtraction",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "properties": {
                            "supplier_name": {"type": "string"},
                            "date": {"type": "string"},
                            "amount": {"type": "string"},
                            "due_date": {"type": "string"},
                            "description": {"type": "string"},
                            "category": {"type": "string"},
                            "notes": {"type": "string"},
                        },
                        "required": [
                            "supplier_name",
                            "date",
                            "amount",
                            "due_date",
                            "description",
                            "category",
                            "notes",
                        ],
                        "additionalProperties": False,
                    },
                },
            },
        )
    content_text = response.choices[0].message.content or "{}"
    parsed = json.loads(content_text)
    return {**_empty_document_fields(), **parsed}


def _save_meeting_summary(record: dict[str, Any]) -> dict[str, Any]:
    meetings = _read_json_list(MEETINGS_FILE)
    payload = {
        "id": f"meeting-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}",
        "created_at": datetime.utcnow().isoformat(),
        **record,
    }
    meetings.append(payload)
    _write_json_list(MEETINGS_FILE, meetings)
    return payload


def _replace_meeting_summary(record: dict[str, Any]) -> None:
    record_id = record.get("id")
    if not record_id:
        return
    meetings = _read_json_list(MEETINGS_FILE)
    updated = False
    for index, meeting in enumerate(meetings):
        if meeting.get("id") == record_id:
            meetings[index] = record
            updated = True
            break
    if updated:
        _write_json_list(MEETINGS_FILE, meetings)


def _read_json_list(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def _write_json_list(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def _pending_action_count(reports: list[dict[str, Any]]) -> int:
    count = 0
    for report in reports:
        path_value = report.get("path")
        if not path_value:
            continue
        try:
            content = json.loads(Path(path_value).read_text(encoding="utf-8"))
        except Exception:
            continue
        body = content.get("content") if isinstance(content, dict) else {}
        if isinstance(body, dict):
            count += len(body.get("actions_requiring_approval") or [])
    return count


def _product_row(product: models.Product) -> dict[str, Any]:
    status = "normal"
    if product.current_stock < product.minimum_stock:
        status = "low_stock"
    elif product.expiration_date and date.today() <= product.expiration_date <= date.today() + timedelta(days=30):
        status = "near_expiration"
    return {
        "id": product.id,
        "sku": product.sku,
        "name": product.name,
        "category": product.category,
        "current_stock": product.current_stock,
        "minimum_stock": product.minimum_stock,
        "ideal_stock": product.ideal_stock,
        "criticality": product.criticality,
        "expiration_date": product.expiration_date.isoformat() if product.expiration_date else None,
        "supplier_id": product.supplier_id,
        "status": status,
    }


def _alert_row(alert: models.StockAlert) -> dict[str, Any]:
    return {
        "id": alert.id,
        "product_id": alert.product_id,
        "alert_type": alert.alert_type,
        "severity": alert.severity,
        "title": alert.title,
        "description": alert.description,
        "data": _parse_json(alert.data_json),
        "created_at": alert.created_at.isoformat(),
    }


def _supplier_row(supplier: models.Supplier) -> dict[str, Any]:
    missing = []
    if not supplier.email:
        missing.append("email")
    if not supplier.phone:
        missing.append("phone")
    return {
        "id": supplier.id,
        "name": supplier.name,
        "email": supplier.email,
        "phone": supplier.phone,
        "missing": missing,
        "default_lead_time_days": supplier.default_lead_time_days,
    }


def _empty_document_fields() -> dict[str, str]:
    return {
        "supplier_name": "",
        "date": "",
        "amount": "",
        "due_date": "",
        "description": "",
        "category": "",
        "notes": "",
    }


def _parse_json(value: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _extract_json(text: str) -> str:
    stripped = text.strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end != -1 and end > start:
        return stripped[start : end + 1]
    return stripped


def _looks_like_low_stock(query: str, scope: str) -> bool:
    return scope == "baixo_estoque" or any(term in query for term in ("baixo", "menor estoque", "falta", "critico"))


def _looks_like_expiration(query: str, scope: str) -> bool:
    return scope == "vencimentos" or any(term in query for term in ("venc", "validade", "expira"))


def _looks_like_abnormal(query: str, scope: str) -> bool:
    return scope == "consumo" or any(term in query for term in ("consumo", "anormal", "movimentacao"))


def _looks_like_supplier(query: str, scope: str) -> bool:
    return scope == "fornecedores" or any(term in query for term in ("fornecedor", "contato", "email", "telefone"))
