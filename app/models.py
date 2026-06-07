from datetime import date, datetime
from typing import Optional

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utcnow() -> datetime:
    return datetime.utcnow()


class Supplier(Base):
    __tablename__ = "suppliers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    contact_name: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    email: Mapped[Optional[str]] = mapped_column(String(160), nullable=True)
    phone: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    default_lead_time_days: Mapped[int] = mapped_column(Integer, default=7)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    products: Mapped[list["Product"]] = relationship(back_populates="supplier")


class Product(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    sku: Mapped[str] = mapped_column(String(60), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(180), nullable=False, index=True)
    normalized_name: Mapped[str] = mapped_column(String(180), nullable=False, index=True)
    category: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    unit: Mapped[str] = mapped_column(String(40), nullable=False)
    current_stock: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    minimum_stock: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ideal_stock: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    average_unit_cost: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    supplier_id: Mapped[Optional[int]] = mapped_column(ForeignKey("suppliers.id"), nullable=True)
    criticality: Mapped[str] = mapped_column(String(20), nullable=False, default="medium")
    expiration_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    supplier: Mapped[Optional[Supplier]] = relationship(back_populates="products")
    movements: Mapped[list["InventoryMovement"]] = relationship(back_populates="product")
    alerts: Mapped[list["StockAlert"]] = relationship(back_populates="product")


class InventoryMovement(Base):
    __tablename__ = "inventory_movements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False, index=True)
    movement_type: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(String(200), nullable=False)
    source: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    responsible_name: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    product: Mapped[Product] = relationship(back_populates="movements")


class StockRule(Base):
    __tablename__ = "stock_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False, index=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    rule_type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    parameters_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class StockAlert(Base):
    __tablename__ = "stock_alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    product_id: Mapped[Optional[int]] = mapped_column(ForeignKey("products.id"), nullable=True, index=True)
    alert_type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(180), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    data_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="open", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    product: Mapped[Optional[Product]] = relationship(back_populates="alerts")
