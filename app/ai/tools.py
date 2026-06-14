from __future__ import annotations

import json
import unicodedata
from datetime import date, datetime, timedelta
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
DOCUMENTS_FILE = BASE_DIR / "operational_data" / "documents.json"


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
        "products": [_product_row(product) for product in products],
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


def search_inventory_items_tool(
    db: Session,
    starts_with: str | None = None,
    contains: str | None = None,
    sku: str | None = None,
    category: str | None = None,
    low_stock_only: bool = False,
    expiration_days: int | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    limit = _safe_limit(limit)
    today = date.today()
    products = db.scalars(select(models.Product).where(models.Product.active.is_(True)).order_by(models.Product.name)).all()
    prefix = _normalize_text(starts_with or "")
    needle = _normalize_text(contains or "")
    sku_filter = _normalize_text(sku or "")
    category_filter = _normalize_text(category or "")
    max_expiration = None
    if expiration_days is not None:
        max_expiration = today + timedelta(days=max(0, min(int(expiration_days), 365)))

    rows = []
    for product in products:
        normalized_name = _normalize_text(product.name)
        normalized_sku = _normalize_text(product.sku)
        normalized_category = _normalize_text(product.category)
        if prefix and not (normalized_name.startswith(prefix) or normalized_sku.startswith(prefix)):
            continue
        if needle and needle not in f"{normalized_name} {normalized_sku} {normalized_category}":
            continue
        if sku_filter and sku_filter not in normalized_sku:
            continue
        if category_filter and category_filter not in normalized_category:
            continue
        if low_stock_only and product.current_stock >= product.minimum_stock:
            continue
        if max_expiration is not None and not (
            product.expiration_date and today <= product.expiration_date <= max_expiration
        ):
            continue
        rows.append(_product_row(product))

    return {
        "count": len(rows),
        "filters": {
            "starts_with": starts_with,
            "contains": contains,
            "sku": sku,
            "category": category,
            "low_stock_only": low_stock_only,
            "expiration_days": expiration_days,
            "limit": limit,
        },
        "items": rows[:limit],
    }


def fuzzy_search_inventory_tool(
    db: Session,
    query: str,
    limit: int = 50,
) -> dict[str, Any]:
    """Busca inteligente por nome, letra ou trecho. Tenta multiplas estrategias automaticamente."""
    limit = _safe_limit(limit)
    query_norm = _normalize_text(query)
    if not query_norm:
        return {"count": 0, "query": query, "strategy": "empty", "items": []}

    products = db.scalars(
        select(models.Product)
        .where(models.Product.active.is_(True))
        .order_by(models.Product.name)
    ).all()

    seen_ids: set[int] = set()
    rows: list[dict[str, Any]] = []
    strategy_used = ""

    if len(query_norm) == 1:
        # Letra unica: starts_with (contains seria largo demais)
        for product in products:
            norm_name = _normalize_text(product.name)
            norm_sku = _normalize_text(product.sku)
            if norm_name.startswith(query_norm) or norm_sku.startswith(query_norm):
                seen_ids.add(product.id)
                rows.append(_product_row(product))
        strategy_used = "starts_with_single_char"
    else:
        # Termo longo: contains primeiro (mais especifico)
        for product in products:
            norm_name = _normalize_text(product.name)
            norm_sku = _normalize_text(product.sku)
            norm_cat = _normalize_text(product.category)
            if query_norm in norm_name or query_norm in norm_sku or query_norm in norm_cat:
                seen_ids.add(product.id)
                rows.append(_product_row(product))
        strategy_used = "contains"

        # Se nao encontrou nada, encurta progressivamente ate achar
        if not rows:
            for length in range(len(query_norm) - 1, 0, -1):
                prefix = query_norm[:length]
                for product in products:
                    if product.id in seen_ids:
                        continue
                    norm_name = _normalize_text(product.name)
                    norm_sku = _normalize_text(product.sku)
                    if norm_name.startswith(prefix) or norm_sku.startswith(prefix):
                        seen_ids.add(product.id)
                        rows.append(_product_row(product))
                if rows:
                    strategy_used = f"starts_with_prefix_{length}"
                    break

    return {
        "count": len(rows),
        "query": query,
        "strategy": strategy_used,
        "items": rows[:limit],
    }


def get_inventory_item_tool(
    db: Session,
    product_id: int | None = None,
    sku: str | None = None,
) -> dict[str, Any]:
    product = None
    if product_id is not None:
        product = db.get(models.Product, product_id)
    elif sku:
        product = db.scalar(select(models.Product).where(models.Product.sku == sku))

    if not product:
        return {"found": False, "product": None, "message": "Produto nao encontrado."}

    supplier = db.get(models.Supplier, product.supplier_id) if product.supplier_id else None
    return {
        "found": True,
        "product": _product_row(product),
        "supplier": _supplier_row(supplier) if supplier else None,
    }


def list_suppliers_tool(
    db: Session,
    name_contains: str | None = None,
    missing_contact_only: bool = False,
    limit: int = 50,
) -> dict[str, Any]:
    limit = _safe_limit(limit)
    suppliers = db.scalars(select(models.Supplier).order_by(models.Supplier.name)).all()
    needle = _normalize_text(name_contains or "")
    rows = []
    for supplier in suppliers:
        if needle and needle not in _normalize_text(supplier.name):
            continue
        if missing_contact_only and supplier.email and supplier.phone:
            continue
        rows.append(_supplier_row(supplier))
    return {
        "count": len(rows),
        "filters": {
            "name_contains": name_contains,
            "missing_contact_only": missing_contact_only,
            "limit": limit,
        },
        "suppliers": rows[:limit],
    }


def list_stock_alerts_tool(
    db: Session,
    alert_type: str | None = None,
    severity: str | None = None,
    status: str | None = "open",
    limit: int = 50,
) -> dict[str, Any]:
    limit = _safe_limit(limit)
    stmt = select(models.StockAlert).order_by(models.StockAlert.created_at.desc())
    if status:
        stmt = stmt.where(models.StockAlert.status == status)
    if alert_type:
        stmt = stmt.where(models.StockAlert.alert_type == alert_type)
    if severity:
        stmt = stmt.where(models.StockAlert.severity == severity)
    alerts = db.scalars(stmt).all()
    return {
        "count": len(alerts),
        "filters": {"alert_type": alert_type, "severity": severity, "status": status, "limit": limit},
        "alerts": [_alert_row(alert) for alert in alerts[:limit]],
    }


def list_saved_documents_tool(
    due_in_days: int | None = None,
    supplier_contains: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    limit = _safe_limit(limit)
    documents = _read_json_list(DOCUMENTS_FILE)
    today = date.today()
    max_due = today + timedelta(days=max(0, min(int(due_in_days), 365))) if due_in_days is not None else None
    needle = _normalize_text(supplier_contains or "")
    rows = []
    for document in documents:
        if needle and needle not in _normalize_text(document.get("supplier_name") or ""):
            continue
        if max_due is not None:
            try:
                due_date = date.fromisoformat(document.get("due_date") or "")
            except ValueError:
                continue
            if not (today <= due_date <= max_due):
                continue
        rows.append(document)
    rows.sort(key=lambda item: item.get("due_date") or item.get("created_at") or "")
    return {
        "count": len(rows),
        "filters": {"due_in_days": due_in_days, "supplier_contains": supplier_contains, "limit": limit},
        "documents": rows[:limit],
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


OPERATIONAL_TOOL_DEFINITIONS: list[dict[str, Any]] = [
    _strict_function_tool(
        "fuzzy_search_inventory_tool",
        "Busca inteligente de produtos por nome, letra ou trecho. Use SEMPRE que o usuario perguntar sobre itens por nome, letra inicial ou palavra-chave. Tenta multiplas estrategias automaticamente: letra unica usa starts_with, termo longo usa contains e encurta progressivamente se necessario.",
        {
            "query": {"type": "string", "description": "Letra, palavra ou trecho a buscar. Exemplos: 'a', 'algodao', 'seringa descartavel'."},
            "limit": {"type": "integer", "description": "Limite de resultados entre 1 e 200."},
        },
        ["query", "limit"],
    ),
    _strict_function_tool(
        "search_inventory_items_tool",
        "Busca itens do estoque com filtros combinados: SKU exato, categoria, baixo estoque ou validade proxima. Use quando o usuario pedir filtros especificos, nao para busca por nome.",
        {
            "starts_with": {"type": ["string", "null"], "description": "Prefixo de nome ou SKU."},
            "contains": {"type": ["string", "null"], "description": "Trecho livre para buscar em nome, SKU ou categoria."},
            "sku": {"type": ["string", "null"], "description": "SKU inteiro ou parcial."},
            "category": {"type": ["string", "null"], "description": "Categoria inteira ou parcial."},
            "low_stock_only": {"type": "boolean", "description": "True para retornar apenas itens abaixo do minimo."},
            "expiration_days": {"type": ["integer", "null"], "description": "Janela de vencimento em dias ou null."},
            "limit": {"type": "integer", "description": "Limite de resultados entre 1 e 200."},
        },
        ["starts_with", "contains", "sku", "category", "low_stock_only", "expiration_days", "limit"],
    ),
    _strict_function_tool(
        "get_inventory_item_tool",
        "Consulta um produto especifico por ID ou SKU.",
        {
            "product_id": {"type": ["integer", "null"], "description": "ID do produto ou null."},
            "sku": {"type": ["string", "null"], "description": "SKU do produto ou null."},
        },
        ["product_id", "sku"],
    ),
    _strict_function_tool(
        "list_suppliers_tool",
        "Lista fornecedores, com filtro por nome e por contato incompleto.",
        {
            "name_contains": {"type": ["string", "null"], "description": "Trecho do nome do fornecedor ou null."},
            "missing_contact_only": {"type": "boolean", "description": "True para listar apenas fornecedores sem email ou telefone."},
            "limit": {"type": "integer", "description": "Limite de resultados entre 1 e 200."},
        },
        ["name_contains", "missing_contact_only", "limit"],
    ),
    _strict_function_tool(
        "list_stock_alerts_tool",
        "Lista alertas de estoque por tipo, severidade e status.",
        {
            "alert_type": {"type": ["string", "null"], "description": "Tipo do alerta ou null."},
            "severity": {"type": ["string", "null"], "description": "Severidade ou null."},
            "status": {"type": ["string", "null"], "description": "Status do alerta. Use open para abertos."},
            "limit": {"type": "integer", "description": "Limite de resultados entre 1 e 200."},
        },
        ["alert_type", "severity", "status", "limit"],
    ),
    _strict_function_tool(
        "list_saved_documents_tool",
        "Lista documentos, contas, boletos, notas e anexos salvos no arquivo operacional local. Use para perguntas sobre pagamento, vencimento, fornecedor ou comprovante aproximado.",
        {
            "due_in_days": {"type": ["integer", "null"], "description": "Janela de vencimento em dias ou null."},
            "supplier_contains": {"type": ["string", "null"], "description": "Trecho do fornecedor ou null."},
            "limit": {"type": "integer", "description": "Limite de resultados entre 1 e 200."},
        },
        ["due_in_days", "supplier_contains", "limit"],
    ),
    _strict_function_tool(
        "run_stock_check_tool",
        "Executa checagem deterministica de estoque e retorna alertas gerados/encontrados.",
        {},
        [],
    ),
]


WHITELISTED_TOOLS: dict[str, ToolFunction] = {
    "list_products_tool": list_products_tool,
    "list_open_alerts_tool": list_open_alerts_tool,
    "run_stock_check_tool": run_stock_check_tool,
    "get_product_movements_tool": get_product_movements_tool,
    "get_supplier_tool": get_supplier_tool,
    "fuzzy_search_inventory_tool": fuzzy_search_inventory_tool,
    "search_inventory_items_tool": search_inventory_items_tool,
    "get_inventory_item_tool": get_inventory_item_tool,
    "list_suppliers_tool": list_suppliers_tool,
    "list_stock_alerts_tool": list_stock_alerts_tool,
    "list_saved_documents_tool": list_saved_documents_tool,
    "create_ai_report_tool": create_ai_report_tool,
    "register_ai_log_tool": register_ai_log_tool,
}


DB_TOOLS = {
    "list_products_tool",
    "list_open_alerts_tool",
    "run_stock_check_tool",
    "get_product_movements_tool",
    "get_supplier_tool",
    "fuzzy_search_inventory_tool",
    "search_inventory_items_tool",
    "get_inventory_item_tool",
    "list_suppliers_tool",
    "list_stock_alerts_tool",
}


def _allowed_args_for_tool(tool_name: str) -> set[str]:
    for tool in [*TOOL_DEFINITIONS, *OPERATIONAL_TOOL_DEFINITIONS]:
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
    if tool_name in {"fuzzy_search_inventory_tool", "search_inventory_items_tool", "list_suppliers_tool", "list_stock_alerts_tool", "list_saved_documents_tool"}:
        args["limit"] = _safe_limit(args.get("limit") or 50)
    if tool_name == "search_inventory_items_tool":
        args["low_stock_only"] = bool(args.get("low_stock_only"))
    if tool_name == "list_suppliers_tool":
        args["missing_contact_only"] = bool(args.get("missing_contact_only"))

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


def run_operational_tool(db: Session, tool_name: str, tool_args: dict[str, Any] | None = None) -> dict[str, Any]:
    allowed = {tool["function"]["name"] for tool in OPERATIONAL_TOOL_DEFINITIONS}
    if tool_name not in allowed:
        return {"ok": False, "tool_name": tool_name, "error": f"Tool operacional nao permitida: {tool_name}"}
    return run_tool(db, tool_name, tool_args)


def _safe_limit(value: Any, default: int = 50) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(1, min(parsed, 200))


def _normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    return " ".join(normalized.lower().split())


def _read_json_list(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


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
        "unit": product.unit,
        "current_stock": product.current_stock,
        "minimum_stock": product.minimum_stock,
        "ideal_stock": product.ideal_stock,
        "criticality": product.criticality,
        "expiration_date": product.expiration_date.isoformat() if product.expiration_date else None,
        "supplier_id": product.supplier_id,
        "status": status,
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
        "contact_name": supplier.contact_name,
        "email": supplier.email,
        "phone": supplier.phone,
        "missing": missing,
        "default_lead_time_days": supplier.default_lead_time_days,
        "notes": supplier.notes,
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
        "status": alert.status,
        "created_at": alert.created_at.isoformat(),
    }
