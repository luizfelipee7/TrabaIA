from datetime import date, datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


Criticality = Literal["low", "medium", "high"]
MovementType = Literal["in", "out", "adjustment", "loss"]
Severity = Literal["low", "medium", "high"]
AlertStatus = Literal["open", "ignored", "resolved"]


class SupplierBase(BaseModel):
    name: str
    contact_name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    default_lead_time_days: int = 7
    notes: Optional[str] = None


class SupplierCreate(SupplierBase):
    pass


class SupplierRead(SupplierBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime


class ProductBase(BaseModel):
    sku: str
    name: str
    normalized_name: Optional[str] = None
    category: str
    unit: str
    current_stock: int = Field(default=0, ge=0)
    minimum_stock: int = Field(default=0, ge=0)
    ideal_stock: int = Field(default=0, ge=0)
    average_unit_cost: float = Field(default=0, ge=0)
    supplier_id: Optional[int] = None
    criticality: Criticality = "medium"
    expiration_date: Optional[date] = None
    active: bool = True


class ProductCreate(ProductBase):
    pass


class ProductUpdate(BaseModel):
    sku: Optional[str] = None
    name: Optional[str] = None
    normalized_name: Optional[str] = None
    category: Optional[str] = None
    unit: Optional[str] = None
    current_stock: Optional[int] = Field(default=None, ge=0)
    minimum_stock: Optional[int] = Field(default=None, ge=0)
    ideal_stock: Optional[int] = Field(default=None, ge=0)
    average_unit_cost: Optional[float] = Field(default=None, ge=0)
    supplier_id: Optional[int] = None
    criticality: Optional[Criticality] = None
    expiration_date: Optional[date] = None
    active: Optional[bool] = None


class ProductRead(ProductBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    normalized_name: str
    created_at: datetime
    updated_at: datetime


class MovementBase(BaseModel):
    product_id: int
    movement_type: MovementType
    quantity: int
    reason: str
    source: Optional[str] = None
    responsible_name: Optional[str] = None
    occurred_at: Optional[datetime] = None


class MovementCreate(MovementBase):
    allow_negative: bool = False


class MovementRead(MovementBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    occurred_at: datetime
    created_at: datetime


class StockRuleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str
    rule_type: str
    parameters_json: str
    active: bool
    created_at: datetime


class StockAlertRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    product_id: Optional[int]
    alert_type: str
    severity: Severity
    title: str
    description: str
    data_json: str
    status: AlertStatus
    created_at: datetime
    resolved_at: Optional[datetime]


class StockCheckAlertSummary(BaseModel):
    type: str
    severity: str
    product: Optional[str] = None
    description: str


class StockCheckSummary(BaseModel):
    checked_products: int
    alerts_created: int
    alerts: list[StockCheckAlertSummary]


class SeedSummary(BaseModel):
    suppliers: int
    products: int
    movements: int
    rules: int


class Message(BaseModel):
    message: str
    data: dict[str, Any] | None = None
