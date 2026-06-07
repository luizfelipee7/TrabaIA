from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models, schemas, seed, services
from app.database import get_db


router = APIRouter()


@router.get("/products", response_model=list[schemas.ProductRead])
def list_products(db: Session = Depends(get_db)):
    return db.scalars(select(models.Product).order_by(models.Product.name)).all()


@router.get("/products/{product_id}", response_model=schemas.ProductRead)
def get_product(product_id: int, db: Session = Depends(get_db)):
    return services.get_product_or_404(db, product_id)


@router.post("/products", response_model=schemas.ProductRead, status_code=201)
def create_product(payload: schemas.ProductCreate, db: Session = Depends(get_db)):
    return services.create_product(db, payload)


@router.patch("/products/{product_id}", response_model=schemas.ProductRead)
def update_product(product_id: int, payload: schemas.ProductUpdate, db: Session = Depends(get_db)):
    return services.update_product(db, product_id, payload)


@router.get("/suppliers", response_model=list[schemas.SupplierRead])
def list_suppliers(db: Session = Depends(get_db)):
    return db.scalars(select(models.Supplier).order_by(models.Supplier.name)).all()


@router.get("/suppliers/{supplier_id}", response_model=schemas.SupplierRead)
def get_supplier(supplier_id: int, db: Session = Depends(get_db)):
    return services.get_supplier_or_404(db, supplier_id)


@router.post("/suppliers", response_model=schemas.SupplierRead, status_code=201)
def create_supplier(payload: schemas.SupplierCreate, db: Session = Depends(get_db)):
    supplier = models.Supplier(**payload.model_dump())
    db.add(supplier)
    db.commit()
    db.refresh(supplier)
    return supplier


@router.get("/movements", response_model=list[schemas.MovementRead])
def list_movements(db: Session = Depends(get_db)):
    return db.scalars(
        select(models.InventoryMovement).order_by(models.InventoryMovement.occurred_at.desc())
    ).all()


@router.get("/products/{product_id}/movements", response_model=list[schemas.MovementRead])
def list_product_movements(product_id: int, db: Session = Depends(get_db)):
    services.get_product_or_404(db, product_id)
    return db.scalars(
        select(models.InventoryMovement)
        .where(models.InventoryMovement.product_id == product_id)
        .order_by(models.InventoryMovement.occurred_at.desc())
    ).all()


@router.post("/movements", response_model=schemas.MovementRead, status_code=201)
def create_movement(payload: schemas.MovementCreate, db: Session = Depends(get_db)):
    return services.create_movement(db, payload)


@router.get("/rules", response_model=list[schemas.StockRuleRead])
def list_rules(db: Session = Depends(get_db)):
    return db.scalars(select(models.StockRule).order_by(models.StockRule.name)).all()


@router.get("/alerts", response_model=list[schemas.StockAlertRead])
def list_alerts(db: Session = Depends(get_db)):
    return db.scalars(select(models.StockAlert).order_by(models.StockAlert.created_at.desc())).all()


@router.get("/alerts/open", response_model=list[schemas.StockAlertRead])
def list_open_alerts(db: Session = Depends(get_db)):
    return db.scalars(
        select(models.StockAlert)
        .where(models.StockAlert.status == "open")
        .order_by(models.StockAlert.created_at.desc())
    ).all()


@router.patch("/alerts/{alert_id}/resolve", response_model=schemas.StockAlertRead)
def resolve_alert(alert_id: int, db: Session = Depends(get_db)):
    return services.set_alert_status(db, alert_id, "resolved")


@router.patch("/alerts/{alert_id}/ignore", response_model=schemas.StockAlertRead)
def ignore_alert(alert_id: int, db: Session = Depends(get_db)):
    return services.set_alert_status(db, alert_id, "ignored")


@router.post("/seed/reset", response_model=schemas.SeedSummary)
def reset_seed(db: Session = Depends(get_db)):
    return seed.reset_and_seed(db)


@router.post("/simulation/run-stock-check", response_model=schemas.StockCheckSummary)
def run_stock_check(db: Session = Depends(get_db)):
    return services.run_stock_check(db)
