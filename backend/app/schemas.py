"""Pydantic models.

Request models are used for validation on the hot paths. Response models exist for
documentation and cold paths only — hot responses are hand-built dicts served
through ORJSONResponse (research R7). Field names match contracts/openapi.yaml exactly.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class StartTransactionRequest(BaseModel):
    stationId: str = Field(min_length=1)


class ScanItemRequest(BaseModel):
    sku: str = Field(min_length=1)


class CatalogItemOut(BaseModel):
    sku: str
    name: str
    price: float


class CatalogResponse(BaseModel):
    items: list[CatalogItemOut]


class TransactionOut(BaseModel):
    transactionId: str
    stationId: str
    status: str
    itemCount: int
    runningTotal: float
    startedAt: str


class ScanResult(BaseModel):
    transactionId: str
    sku: str
    name: str
    unitPrice: float
    itemCount: int
    runningTotal: float


class ReceiptLine(BaseModel):
    sku: str
    name: str
    unitPrice: float
    quantity: int


class Receipt(BaseModel):
    transactionId: str
    stationId: str
    itemCount: int
    totalAmount: float
    startedAt: str
    completedAt: str
    lines: list[ReceiptLine]


class LowStockAlertOut(BaseModel):
    sku: str
    name: str
    currentStock: int
    threshold: int
    triggeredAt: str


class LowStockResponse(BaseModel):
    threshold: int
    generatedAt: str
    alerts: list[LowStockAlertOut]


class PopularItem(BaseModel):
    sku: str
    name: str
    scanCount: int
    rank: int


class PopularItemsResponse(BaseModel):
    windowSize: int
    slideInterval: int
    windowStart: int
    windowEnd: int
    computedAt: str
    items: list[PopularItem]


class ApiErrorOut(BaseModel):
    error: str
    message: str
