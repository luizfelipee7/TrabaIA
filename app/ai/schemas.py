from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class AIModelSelectRequest(BaseModel):
    model_name: str
    attempt_load: bool = False


class AIReviewRequest(BaseModel):
    objective: str | None = None


class AIBatchReviewRequest(BaseModel):
    objective: str | None = None
    count: int = 3


class AIReportScopeValidation(BaseModel):
    passed: bool
    reason: str
    violations: list[str] = Field(default_factory=list)
    correction_instruction: str


class ProductReportItem(BaseModel):
    product_id: int | None = None
    sku: str | None = None
    product_name: str | None = None
    supplier_id: int | None = None
    severity: Literal["low", "medium", "high"] | str
    evidence: str
    recommended_action: str
    requires_approval: bool


class PurchaseSuggestion(ProductReportItem):
    suggested_quantity: int | float | None = None


class SupplierIssue(BaseModel):
    supplier_id: int | None = None
    supplier_name: str | None = None
    severity: Literal["low", "medium", "high"] | str
    evidence: str
    recommended_action: str
    requires_approval: bool
    related_product_ids: list[int] = Field(default_factory=list)


class ApprovalAction(BaseModel):
    action: str
    severity: Literal["low", "medium", "high"] | str
    evidence: str
    approval_reason: str
    related_product_id: int | None = None
    supplier_id: int | None = None


class NextAction(BaseModel):
    action: str
    priority: Literal["low", "medium", "high"] | str
    owner: str | None = None
    evidence: str
    requires_approval: bool = False


class DataQualityIssue(BaseModel):
    issue_type: str
    severity: Literal["low", "medium", "high"] | str
    evidence: str
    recommended_action: str
    related_product_id: int | None = None
    supplier_id: int | None = None


class DailyInventoryReviewReport(BaseModel):
    report_type: Literal["daily_inventory_review"] = "daily_inventory_review"
    generated_at: datetime
    scope: list[str]
    executive_summary: str
    stock_shortages: list[ProductReportItem] = Field(default_factory=list)
    expiration_risks: list[ProductReportItem] = Field(default_factory=list)
    abnormal_consumption: list[ProductReportItem] = Field(default_factory=list)
    supplier_issues: list[SupplierIssue] = Field(default_factory=list)
    purchase_suggestions: list[PurchaseSuggestion] = Field(default_factory=list)
    actions_requiring_approval: list[ApprovalAction] = Field(default_factory=list)
    next_actions: list[NextAction] = Field(default_factory=list)
    data_quality_issues: list[DataQualityIssue] = Field(default_factory=list)


DAILY_INVENTORY_REVIEW_SCHEMA_EXAMPLE: dict[str, Any] = {
    "report_type": "daily_inventory_review",
    "generated_at": "2026-06-07T10:00:00",
    "scope": [
        "stock_shortages",
        "expiration_risks",
        "abnormal_consumption",
        "supplier_issues",
        "purchase_suggestions",
        "actions_requiring_approval",
        "data_quality_issues",
    ],
    "executive_summary": "Resumo operacional curto.",
    "stock_shortages": [],
    "expiration_risks": [],
    "abnormal_consumption": [],
    "supplier_issues": [],
    "purchase_suggestions": [],
    "actions_requiring_approval": [],
    "next_actions": [],
    "data_quality_issues": [],
}
