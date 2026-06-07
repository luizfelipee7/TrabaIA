from __future__ import annotations

import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.database import BASE_DIR


QA_DIR = BASE_DIR / "qa_runs"
QA_RUNS_DIR = QA_DIR / "runs"
QA_BATCHES_DIR = QA_DIR / "batches"


def utcnow_iso() -> str:
    return datetime.utcnow().isoformat()


def enrich_and_save_run(
    result: dict[str, Any],
    *,
    objective: str | None,
    model: str,
    started_at: str,
    finished_at: str,
    duration_ms: int,
    batch_id: str | None = None,
    batch_index: int | None = None,
) -> dict[str, Any]:
    run_id = f"run-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:8]}"
    timeline = build_timeline(result, objective=objective)
    metrics = build_metrics(result, timeline=timeline, duration_ms=duration_ms)
    qa = {
        "run_id": run_id,
        "batch_id": batch_id,
        "batch_index": batch_index,
        "model": model,
        "objective": objective,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_ms": duration_ms,
        "metrics": metrics,
        "timeline": timeline,
        "exported_at": utcnow_iso(),
    }
    enriched = {**result, "qa": qa}
    path = _run_path(run_id)
    _write_json(path, enriched)
    enriched["qa"]["export_path"] = str(path)
    return enriched


def build_metrics(result: dict[str, Any], timeline: list[dict[str, Any]], duration_ms: int) -> dict[str, Any]:
    steps = result.get("steps") or []
    tool_steps = [step for step in steps if step.get("action_type") == "tool_call"]
    final_steps = [step for step in steps if step.get("action_type") in {"final_message", "final_response"}]
    tool_counts = Counter(step.get("tool_name") or "unknown" for step in tool_steps)
    failed_tool_count = 0
    repeated_calls = Counter()

    for step in tool_steps:
        result_payload = step.get("tool_result") or {}
        if isinstance(result_payload, dict) and result_payload.get("ok") is False:
            failed_tool_count += 1
        repeated_calls[_call_signature(step)] += 1

    repeated_tool_call_count = sum(count - 1 for count in repeated_calls.values() if count > 1)
    alerts = extract_alerts(result)
    final_report = result.get("final_report") or {}
    max_step = max([int(step.get("step") or 0) for step in steps] or [0])

    return {
        "status": result.get("status"),
        "duration_ms": duration_ms,
        "model_call_count": max_step,
        "step_event_count": len(steps),
        "timeline_event_count": len(timeline),
        "tool_call_count": len(tool_steps),
        "unique_tool_count": len(tool_counts),
        "tool_counts": dict(sorted(tool_counts.items())),
        "failed_tool_count": failed_tool_count,
        "repeated_tool_call_count": repeated_tool_call_count,
        "skill_event_count": 0,
        "visible_model_message_count": len(final_steps),
        "alert_count": len(alerts),
        "stock_shortage_count": len(final_report.get("stock_shortages") or []),
        "expiration_risk_count": len(final_report.get("expiration_risks") or []),
        "abnormal_consumption_count": len(final_report.get("abnormal_consumption") or []),
        "supplier_issue_count": len(final_report.get("supplier_issues") or []),
        "purchase_suggestion_count": len(final_report.get("purchase_suggestions") or []),
        "approval_action_count": len(final_report.get("actions_requiring_approval") or []),
        "next_action_count": len(final_report.get("next_actions") or []),
        "data_quality_issue_count": len(final_report.get("data_quality_issues") or []),
        "error_count": _count_errors(result, tool_steps),
    }


def build_timeline(result: dict[str, Any], objective: str | None) -> list[dict[str, Any]]:
    timeline = [
        {
            "type": "objective",
            "label": "Objetivo",
            "summary": objective or "Objetivo padrao da revisao diaria.",
            "detail": {"objective": objective},
        }
    ]

    for step in result.get("steps") or []:
        action_type = step.get("action_type")
        if action_type == "tool_call":
            tool_result = step.get("tool_result")
            timeline.append(
                {
                    "type": "tool",
                    "label": step.get("tool_name") or "tool",
                    "summary": _tool_summary(step),
                    "step": step.get("step"),
                    "detail": {
                        "tool_call_id": step.get("tool_call_id"),
                        "tool_name": step.get("tool_name"),
                        "tool_args": step.get("tool_args"),
                        "tool_result": tool_result,
                    },
                    "ok": not (isinstance(tool_result, dict) and tool_result.get("ok") is False),
                }
            )
        elif action_type in {"final_message", "final_response"}:
            timeline.append(
                {
                    "type": "response",
                    "label": "Resposta final",
                    "summary": _short_text(step.get("message") or ""),
                    "step": step.get("step"),
                    "detail": {"message": step.get("message")},
                    "ok": True,
                }
            )
        else:
            timeline.append(
                {
                    "type": "system",
                    "label": action_type or "evento",
                    "summary": _short_text(json.dumps(step, ensure_ascii=False, default=str)),
                    "step": step.get("step"),
                    "detail": step,
                    "ok": True,
                }
            )

    timeline.append(
        {
            "type": "skill",
            "label": "Skills",
            "summary": "Nenhuma skill externa foi chamada por esta execucao.",
            "detail": {"skill_event_count": 0},
            "ok": True,
        }
    )
    return timeline


def extract_alerts(result: dict[str, Any]) -> list[dict[str, Any]]:
    alerts: list[dict[str, Any]] = []
    for step in result.get("steps") or []:
        tool_result = step.get("tool_result")
        if not isinstance(tool_result, dict):
            continue
        payload = tool_result.get("result")
        if isinstance(payload, dict):
            if isinstance(payload.get("alerts"), list):
                alerts.extend(_normalize_alerts(payload["alerts"], source=step.get("tool_name")))
            nested = payload.get("result")
            if isinstance(nested, dict) and isinstance(nested.get("alerts"), list):
                alerts.extend(_normalize_alerts(nested["alerts"], source=step.get("tool_name")))
    return alerts


def save_batch_artifacts(batch_id: str, runs: list[dict[str, Any]], objective: str | None) -> dict[str, Any]:
    QA_BATCHES_DIR.mkdir(parents=True, exist_ok=True)
    summary = {
        "batch_id": batch_id,
        "objective": objective,
        "created_at": utcnow_iso(),
        "run_count": len(runs),
        "runs": [_run_summary(run) for run in runs],
        "aggregate": _aggregate_runs(runs),
    }
    json_path = QA_BATCHES_DIR / f"{batch_id}.json"
    csv_path = QA_BATCHES_DIR / f"{batch_id}.csv"
    _write_json(json_path, summary)
    _write_batch_csv(csv_path, summary["runs"])
    summary["export_paths"] = {"json": str(json_path), "csv": str(csv_path)}
    return summary


def list_runs(limit: int = 50) -> list[dict[str, Any]]:
    if not QA_RUNS_DIR.exists():
        return []
    files = sorted(QA_RUNS_DIR.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    rows = []
    for path in files[:limit]:
        content = _read_json(path)
        rows.append(_run_summary(content, path=path))
    return rows


def list_batches(limit: int = 25) -> list[dict[str, Any]]:
    if not QA_BATCHES_DIR.exists():
        return []
    files = sorted(QA_BATCHES_DIR.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    batches = []
    for path in files[:limit]:
        content = _read_json(path)
        batches.append(
            {
                "batch_id": content.get("batch_id"),
                "created_at": content.get("created_at"),
                "run_count": content.get("run_count"),
                "aggregate": content.get("aggregate"),
                "export_paths": {
                    "json": str(path),
                    "csv": str(path.with_suffix(".csv")),
                },
            }
        )
    return batches


def get_run(run_id: str) -> dict[str, Any] | None:
    path = _run_path(run_id)
    if not path.exists():
        return None
    return _read_json(path)


def get_run_export_path(run_id: str) -> Path | None:
    path = _run_path(run_id)
    return path if path.exists() else None


def get_batch_export_path(batch_id: str, export_format: str) -> Path | None:
    suffix = ".csv" if export_format == "csv" else ".json"
    path = QA_BATCHES_DIR / f"{batch_id}{suffix}"
    return path if path.exists() else None


def _run_path(run_id: str) -> Path:
    safe = "".join(char for char in run_id if char.isalnum() or char in {"-", "_"})
    return QA_RUNS_DIR / f"{safe}.json"


def _run_summary(run: dict[str, Any], path: Path | None = None) -> dict[str, Any]:
    qa = run.get("qa") or {}
    metrics = qa.get("metrics") or {}
    return {
        "run_id": qa.get("run_id"),
        "batch_id": qa.get("batch_id"),
        "batch_index": qa.get("batch_index"),
        "status": run.get("status"),
        "model": qa.get("model") or run.get("model"),
        "started_at": qa.get("started_at"),
        "finished_at": qa.get("finished_at"),
        "duration_ms": metrics.get("duration_ms"),
        "tool_call_count": metrics.get("tool_call_count"),
        "unique_tool_count": metrics.get("unique_tool_count"),
        "failed_tool_count": metrics.get("failed_tool_count"),
        "alert_count": metrics.get("alert_count"),
        "stock_shortage_count": metrics.get("stock_shortage_count"),
        "expiration_risk_count": metrics.get("expiration_risk_count"),
        "abnormal_consumption_count": metrics.get("abnormal_consumption_count"),
        "supplier_issue_count": metrics.get("supplier_issue_count"),
        "purchase_suggestion_count": metrics.get("purchase_suggestion_count"),
        "approval_action_count": metrics.get("approval_action_count"),
        "data_quality_issue_count": metrics.get("data_quality_issue_count"),
        "export_path": str(path) if path else qa.get("export_path"),
    }


def _aggregate_runs(runs: list[dict[str, Any]]) -> dict[str, Any]:
    statuses = Counter(run.get("status") for run in runs)
    tool_counts: Counter[str] = Counter()
    durations = []
    for run in runs:
        metrics = (run.get("qa") or {}).get("metrics") or {}
        tool_counts.update(metrics.get("tool_counts") or {})
        if isinstance(metrics.get("duration_ms"), int):
            durations.append(metrics["duration_ms"])
    return {
        "status_counts": dict(statuses),
        "tool_counts": dict(sorted(tool_counts.items())),
        "avg_duration_ms": round(sum(durations) / len(durations), 2) if durations else 0,
        "min_duration_ms": min(durations) if durations else 0,
        "max_duration_ms": max(durations) if durations else 0,
    }


def _write_batch_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "run_id",
        "batch_id",
        "batch_index",
        "status",
        "model",
        "started_at",
        "finished_at",
        "duration_ms",
        "tool_call_count",
        "unique_tool_count",
        "failed_tool_count",
        "alert_count",
        "stock_shortage_count",
        "expiration_risk_count",
        "abnormal_consumption_count",
        "supplier_issue_count",
        "purchase_suggestion_count",
        "approval_action_count",
        "data_quality_issue_count",
        "export_path",
    ]
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key) for key in fieldnames})


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _call_signature(step: dict[str, Any]) -> str:
    return json.dumps(
        {"tool_name": step.get("tool_name"), "tool_args": step.get("tool_args")},
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )


def _count_errors(result: dict[str, Any], tool_steps: list[dict[str, Any]]) -> int:
    count = 0 if result.get("status") == "completed" else 1
    for step in tool_steps:
        payload = step.get("tool_result")
        if isinstance(payload, dict) and payload.get("ok") is False:
            count += 1
    return count


def _tool_summary(step: dict[str, Any]) -> str:
    tool_name = step.get("tool_name") or "tool"
    args = step.get("tool_args") or {}
    result = step.get("tool_result") or {}
    ok = "ok" if not (isinstance(result, dict) and result.get("ok") is False) else "erro"
    return f"{tool_name}({json.dumps(args, ensure_ascii=False, default=str)}) -> {ok}"


def _normalize_alerts(alerts: list[Any], source: str | None) -> list[dict[str, Any]]:
    normalized = []
    for alert in alerts:
        if isinstance(alert, dict):
            normalized.append({**alert, "source_tool": source})
        else:
            normalized.append({"description": str(alert), "source_tool": source})
    return normalized


def _short_text(value: str, limit: int = 220) -> str:
    compact = " ".join(str(value).split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3] + "..."
