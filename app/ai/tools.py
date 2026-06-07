from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models, services
from app.ai.schemas import DailyInventoryReviewReport
from app.database import BASE_DIR


REPORTS_DIR = BASE_DIR / "reports"
LOGS_DIR = BASE_DIR / "logs"
AI_LOG_FILE = LOGS_DIR / "ai_actions.jsonl"


def _json_safe(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=_json_safe), encoding="utf-8")


def list_products_tool(db: Session) -> dict[str, Any]:
    products = db.scalars(select(models.Product).order_by(models.Product.name)).all()
    return {
        "count": len(products),
        "products": [
            {
                "id": product.id,
                "sku": product.sku,
                "name": product.name,
                "category": product.category,
                "current_stock": product.current_stock,
                "minimum_stock": product.minimum_stock,
                "ideal_stock": product.ideal_stock,
                "criticality": product.criticality,
                "expiration_date": product.expiration_date.isoformat()
                if product.expiration_date
                else None,
                "supplier_id": product.supplier_id,
            }
            for product in products
        ],
    }


def list_open_alerts_tool(db: Session) -> dict[str, Any]:
    alerts = db.scalars(
        select(models.StockAlert)
        .where(models.StockAlert.status == "open")
        .order_by(models.StockAlert.created_at.desc())
    ).all()
    return {
        "count": len(alerts),
        "alerts": [
            {
                "id": alert.id,
                "product_id": alert.product_id,
                "alert_type": alert.alert_type,
                "severity": alert.severity,
                "title": alert.title,
                "description": alert.description,
                "data": _parse_json(alert.data_json),
                "created_at": alert.created_at.isoformat(),
            }
            for alert in alerts
        ],
    }


def run_stock_check_tool(db: Session) -> dict[str, Any]:
    return services.run_stock_check(db)


def get_product_movements_tool(db: Session, product_id: int, days: int = 30) -> dict[str, Any]:
    if days < 1:
        days = 1
    if days > 90:
        days = 90

    product = services.get_product_or_404(db, product_id)
    since = datetime.utcnow() - timedelta(days=days)
    movements = db.scalars(
        select(models.InventoryMovement)
        .where(
            models.InventoryMovement.product_id == product_id,
            models.InventoryMovement.occurred_at >= since,
        )
        .order_by(models.InventoryMovement.occurred_at.desc())
    ).all()
    return {
        "product": {"id": product.id, "sku": product.sku, "name": product.name},
        "days": days,
        "count": len(movements),
        "movements": [
            {
                "id": movement.id,
                "movement_type": movement.movement_type,
                "quantity": movement.quantity,
                "reason": movement.reason,
                "source": movement.source,
                "responsible_name": movement.responsible_name,
                "occurred_at": movement.occurred_at.isoformat(),
            }
            for movement in movements
        ],
    }


def get_supplier_tool(db: Session, supplier_id: int) -> dict[str, Any]:
    supplier = services.get_supplier_or_404(db, supplier_id)
    return {
        "id": supplier.id,
        "name": supplier.name,
        "contact_name": supplier.contact_name,
        "email": supplier.email,
        "phone": supplier.phone,
        "default_lead_time_days": supplier.default_lead_time_days,
        "notes": supplier.notes,
        "created_at": supplier.created_at.isoformat(),
    }


def create_ai_report_tool(title: str, content: Any) -> dict[str, Any]:
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Relatorio precisa ser JSON valido: {exc}") from exc

    report = DailyInventoryReviewReport.model_validate(content)
    now = datetime.utcnow()
    safe_title = "".join(char if char.isalnum() else "-" for char in title.lower()).strip("-")
    safe_title = safe_title or "relatorio"
    path = REPORTS_DIR / f"{now.strftime('%Y%m%d-%H%M%S')}-{safe_title[:60]}.json"
    payload = {
        "title": title,
        "created_at": now.isoformat(),
        "content": report.model_dump(mode="json"),
    }
    _write_json(path, payload)
    return {"saved": True, "path": str(path), "title": title}


def register_ai_log_tool(message: str, data: Any | None = None) -> dict[str, Any]:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "created_at": datetime.utcnow().isoformat(),
        "message": message,
        "data": data or {},
    }
    with AI_LOG_FILE.open("a", encoding="utf-8") as file:
        file.write(json.dumps(payload, ensure_ascii=False, default=_json_safe) + "\n")
    return {"saved": True, "path": str(AI_LOG_FILE)}


def read_ai_logs(limit: int = 50) -> list[dict[str, Any]]:
    if not AI_LOG_FILE.exists():
        return []
    lines = AI_LOG_FILE.read_text(encoding="utf-8").splitlines()
    selected = lines[-limit:]
    return [_parse_json(line) for line in selected if line.strip()]


def list_ai_reports(limit: int = 20) -> list[dict[str, Any]]:
    if not REPORTS_DIR.exists():
        return []
    files = sorted(REPORTS_DIR.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    reports = []
    for path in files[:limit]:
        content = _parse_json(path.read_text(encoding="utf-8"))
        reports.append({"path": str(path), "title": content.get("title"), "created_at": content.get("created_at")})
    return reports


def _parse_json(value: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


ToolFunction = Callable[..., dict[str, Any]]


def _strict_function_tool(
    name: str,
    description: str,
    properties: dict[str, Any],
    required: list[str],
) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


TOOL_DEFINITIONS: list[dict[str, Any]] = [
    _strict_function_tool(
        "list_products_tool",
        "Lista produtos resumidos do estoque do consultorio.",
        {},
        [],
    ),
    _strict_function_tool(
        "list_open_alerts_tool",
        "Lista alertas abertos ja persistidos no banco.",
        {},
        [],
    ),
    _strict_function_tool(
        "run_stock_check_tool",
        "Executa a checagem deterministica de estoque e gera alertas quando necessario.",
        {},
        [],
    ),
    _strict_function_tool(
        "get_product_movements_tool",
        "Consulta movimentacoes recentes de um produto.",
        {
            "product_id": {
                "type": "integer",
                "description": "ID numerico do produto.",
            },
            "days": {
                "type": ["integer", "null"],
                "description": "Janela em dias para consultar. Use null para o padrao de 30 dias.",
            },
        },
        ["product_id", "days"],
    ),
    _strict_function_tool(
        "get_supplier_tool",
        "Consulta dados basicos de um fornecedor pelo ID.",
        {
            "supplier_id": {
                "type": "integer",
                "description": "ID numerico do fornecedor.",
            },
        },
        ["supplier_id"],
    ),
    _strict_function_tool(
        "create_ai_report_tool",
        "Salva um relatorio operacional gerado pela IA em arquivo local.",
        {
            "title": {
                "type": "string",
                "description": "Titulo curto do relatorio.",
            },
            "content": {
                "type": "string",
                "description": "Conteudo do relatorio em texto ou JSON serializado.",
            },
        },
        ["title", "content"],
    ),
    _strict_function_tool(
        "register_ai_log_tool",
        "Salva um log operacional curto da acao da IA.",
        {
            "message": {
                "type": "string",
                "description": "Mensagem operacional curta.",
            },
            "data": {
                "type": ["string", "null"],
                "description": "Dados resumidos serializados como texto, ou null.",
            },
        },
        ["message", "data"],
    ),
]


WHITELISTED_TOOLS: dict[str, ToolFunction] = {
    "list_products_tool": list_products_tool,
    "list_open_alerts_tool": list_open_alerts_tool,
    "run_stock_check_tool": run_stock_check_tool,
    "get_product_movements_tool": get_product_movements_tool,
    "get_supplier_tool": get_supplier_tool,
    "create_ai_report_tool": create_ai_report_tool,
    "register_ai_log_tool": register_ai_log_tool,
}


DB_TOOLS = {
    "list_products_tool",
    "list_open_alerts_tool",
    "run_stock_check_tool",
    "get_product_movements_tool",
    "get_supplier_tool",
}


def _allowed_args_for_tool(tool_name: str) -> set[str]:
    for tool in TOOL_DEFINITIONS:
        function = tool["function"]
        if function["name"] == tool_name:
            return set(function["parameters"]["properties"].keys())
    return set()


def run_tool(db: Session, tool_name: str, tool_args: dict[str, Any] | None = None) -> dict[str, Any]:
    if tool_name not in WHITELISTED_TOOLS:
        return {"ok": False, "error": f"Tool nao permitida: {tool_name}"}

    args = tool_args or {}
    unknown_args = sorted(set(args) - _allowed_args_for_tool(tool_name))
    if unknown_args:
        return {
            "ok": False,
            "tool_name": tool_name,
            "error": f"Argumentos nao permitidos para {tool_name}: {unknown_args}",
        }

    if tool_name == "get_product_movements_tool" and args.get("days") is None:
        args["days"] = 30
    if tool_name == "register_ai_log_tool" and args.get("data") is None:
        args["data"] = None

    tool = WHITELISTED_TOOLS[tool_name]
    try:
        if tool_name in DB_TOOLS:
            return {"ok": True, "tool_name": tool_name, "result": tool(db=db, **args)}
        return {"ok": True, "tool_name": tool_name, "result": tool(**args)}
    except Exception as exc:
        return {
            "ok": False,
            "tool_name": tool_name,
            "error": f"Erro ao executar tool: {type(exc).__name__}: {exc}",
        }
