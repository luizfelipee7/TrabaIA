import json
import unicodedata
from datetime import datetime, timedelta
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import models, schemas


def normalize_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    return " ".join(normalized.lower().split())


def get_product_or_404(db: Session, product_id: int) -> models.Product:
    product = db.get(models.Product, product_id)
    if not product:
        raise HTTPException(status_code=404, detail="Produto não encontrado")
    return product


def get_supplier_or_404(db: Session, supplier_id: int) -> models.Supplier:
    supplier = db.get(models.Supplier, supplier_id)
    if not supplier:
        raise HTTPException(status_code=404, detail="Fornecedor não encontrado")
    return supplier


def create_product(db: Session, payload: schemas.ProductCreate) -> models.Product:
    data = payload.model_dump()
    if not data["normalized_name"]:
        data["normalized_name"] = normalize_name(data["name"])
    product = models.Product(**data)
    db.add(product)
    db.commit()
    db.refresh(product)
    return product


def update_product(db: Session, product_id: int, payload: schemas.ProductUpdate) -> models.Product:
    product = get_product_or_404(db, product_id)
    data = payload.model_dump(exclude_unset=True)
    if "name" in data and "normalized_name" not in data:
        data["normalized_name"] = normalize_name(data["name"])
    for key, value in data.items():
        setattr(product, key, value)
    product.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(product)
    return product


def _movement_delta(movement_type: str, quantity: int) -> int:
    if movement_type == "in":
        return quantity
    if movement_type in {"out", "loss"}:
        return -quantity
    if movement_type == "adjustment":
        return quantity
    raise HTTPException(status_code=400, detail="Tipo de movimentação inválido")


def create_movement(db: Session, payload: schemas.MovementCreate) -> models.InventoryMovement:
    if payload.movement_type in {"in", "out", "loss"} and payload.quantity <= 0:
        raise HTTPException(
            status_code=400,
            detail="Movimentações in, out e loss precisam de quantity positivo.",
        )
    if payload.movement_type == "adjustment" and payload.quantity == 0:
        raise HTTPException(status_code=400, detail="Ajuste precisa de quantity diferente de zero.")

    product = get_product_or_404(db, payload.product_id)
    delta = _movement_delta(payload.movement_type, payload.quantity)
    new_stock = product.current_stock + delta
    if new_stock < 0 and not payload.allow_negative:
        raise HTTPException(
            status_code=400,
            detail=f"Movimentação deixaria estoque negativo ({new_stock}). Use allow_negative=true para permitir.",
        )

    product.current_stock = new_stock
    product.updated_at = datetime.utcnow()
    movement = models.InventoryMovement(
        product_id=payload.product_id,
        movement_type=payload.movement_type,
        quantity=payload.quantity,
        reason=payload.reason,
        source=payload.source,
        responsible_name=payload.responsible_name,
        occurred_at=payload.occurred_at or datetime.utcnow(),
    )
    db.add(movement)
    db.commit()
    db.refresh(movement)
    return movement


def _open_alert_exists(
    db: Session,
    alert_type: str,
    product_id: Optional[int],
    title: str,
    data_json: str,
) -> bool:
    stmt = select(models.StockAlert).where(
        models.StockAlert.alert_type == alert_type,
        models.StockAlert.product_id == product_id,
        models.StockAlert.title == title,
        models.StockAlert.status == "open",
        models.StockAlert.data_json == data_json,
    )
    return db.scalar(stmt) is not None


def _create_alert_once(
    db: Session,
    *,
    product_id: Optional[int],
    alert_type: str,
    severity: str,
    title: str,
    description: str,
    data: dict,
) -> tuple[Optional[models.StockAlert], dict]:
    data_json = json.dumps(data, ensure_ascii=False, sort_keys=True)
    summary = {
        "type": alert_type,
        "severity": severity,
        "product": data.get("product_name"),
        "description": description,
    }
    if _open_alert_exists(db, alert_type, product_id, title, data_json):
        return None, summary

    alert = models.StockAlert(
        product_id=product_id,
        alert_type=alert_type,
        severity=severity,
        title=title,
        description=description,
        data_json=data_json,
        status="open",
    )
    db.add(alert)
    return alert, summary


def run_stock_check(db: Session) -> dict:
    now = datetime.utcnow()
    today = now.date()
    products = db.scalars(select(models.Product).where(models.Product.active.is_(True))).all()
    created_alerts: list[models.StockAlert] = []
    summaries: list[dict] = []

    for product in products:
        if product.current_stock < product.minimum_stock:
            is_critical = product.criticality == "high"
            alert, summary = _create_alert_once(
                db,
                product_id=product.id,
                alert_type="critical_low_stock" if is_critical else "low_stock",
                severity="high" if is_critical else "medium",
                title=f"Estoque baixo: {product.name}",
                description=(
                    f"Estoque atual {product.current_stock} abaixo do mínimo {product.minimum_stock}"
                ),
                data={
                    "product_id": product.id,
                    "product_name": product.name,
                    "current_stock": product.current_stock,
                    "minimum_stock": product.minimum_stock,
                    "criticality": product.criticality,
                },
            )
            summaries.append(summary)
            if alert:
                created_alerts.append(alert)

        if product.expiration_date and today <= product.expiration_date <= today + timedelta(days=30):
            days_left = (product.expiration_date - today).days
            alert, summary = _create_alert_once(
                db,
                product_id=product.id,
                alert_type="near_expiration",
                severity="high" if days_left <= 7 else "medium",
                title=f"Vencimento próximo: {product.name}",
                description=f"Produto vence em {days_left} dias ({product.expiration_date.isoformat()})",
                data={
                    "product_id": product.id,
                    "product_name": product.name,
                    "expiration_date": product.expiration_date.isoformat(),
                    "days_left": days_left,
                },
            )
            summaries.append(summary)
            if alert:
                created_alerts.append(alert)

    suppliers = db.scalars(select(models.Supplier)).all()
    for supplier in suppliers:
        missing_email = not supplier.email
        missing_phone = not supplier.phone
        if missing_email or missing_phone:
            missing = []
            if missing_email:
                missing.append("email")
            if missing_phone:
                missing.append("telefone")
            alert, summary = _create_alert_once(
                db,
                product_id=None,
                alert_type="missing_supplier_contact",
                severity="medium",
                title=f"Contato incompleto: {supplier.name}",
                description=f"Fornecedor sem {', '.join(missing)} cadastrado.",
                data={
                    "supplier_id": supplier.id,
                    "supplier_name": supplier.name,
                    "missing": missing,
                },
            )
            summaries.append(summary)
            if alert:
                created_alerts.append(alert)

    for product in products:
        start = now - timedelta(days=30)
        daily_rows = db.execute(
            select(
                func.date(models.InventoryMovement.occurred_at).label("day"),
                func.sum(models.InventoryMovement.quantity).label("total"),
            )
            .where(
                models.InventoryMovement.product_id == product.id,
                models.InventoryMovement.movement_type == "out",
                models.InventoryMovement.occurred_at >= start,
            )
            .group_by(func.date(models.InventoryMovement.occurred_at))
        ).all()
        if len(daily_rows) < 5:
            continue
        totals = [float(row.total or 0) for row in daily_rows]
        historical_average = sum(totals) / len(totals)
        most_recent = max(daily_rows, key=lambda row: row.day)
        most_recent_total = float(most_recent.total or 0)
        if historical_average > 0 and most_recent_total > historical_average * 2:
            alert, summary = _create_alert_once(
                db,
                product_id=product.id,
                alert_type="abnormal_consumption",
                severity="high",
                title=f"Consumo anormal: {product.name}",
                description=(
                    f"Saída recente de {most_recent_total:.0f} acima de 2x a média diária "
                    f"dos últimos 30 dias ({historical_average:.1f})."
                ),
                data={
                    "product_id": product.id,
                    "product_name": product.name,
                    "most_recent_day": str(most_recent.day),
                    "most_recent_total": most_recent_total,
                    "historical_daily_average": round(historical_average, 2),
                },
            )
            summaries.append(summary)
            if alert:
                created_alerts.append(alert)

    db.commit()
    return {
        "checked_products": len(products),
        "alerts_created": len(created_alerts),
        "alerts": summaries,
    }


def set_alert_status(db: Session, alert_id: int, status: str) -> models.StockAlert:
    alert = db.get(models.StockAlert, alert_id)
    if not alert:
        raise HTTPException(status_code=404, detail="Alerta não encontrado")
    alert.status = status
    alert.resolved_at = datetime.utcnow() if status == "resolved" else None
    db.commit()
    db.refresh(alert)
    return alert
