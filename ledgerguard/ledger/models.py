"""Financial data model.

All money fields are integer paise (see ledger/money.py). Pydantic models are
used so that ingestion of external / malformed data fails loudly rather than
silently producing a wrong ledger.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class OrderStatus(str, Enum):
    CREATED = "created"
    PAID = "paid"
    ATTEMPTED = "attempted"


class PaymentStatus(str, Enum):
    CAPTURED = "captured"
    AUTHORIZED = "authorized"
    FAILED = "failed"
    REFUNDED = "refunded"


class RefundStatus(str, Enum):
    PROCESSED = "processed"
    PENDING = "pending"
    FAILED = "failed"


class SettlementStatus(str, Enum):
    PROCESSED = "processed"
    PENDING = "pending"
    FAILED = "failed"


class CreditDebit(str, Enum):
    CREDIT = "credit"
    DEBIT = "debit"


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=False)


class Order(_Record):
    order_id: str
    merchant_id: str
    amount: int                      # paise
    currency: str = "INR"
    created_at: datetime
    status: OrderStatus = OrderStatus.PAID
    customer_reference: str


class Payment(_Record):
    payment_id: str
    order_id: Optional[str]
    amount: int                      # paise, captured amount
    currency: str = "INR"
    status: PaymentStatus = PaymentStatus.CAPTURED
    method: str = "upi"
    captured_at: Optional[datetime]
    fee: int                         # paise, platform fee charged
    tax: int                         # paise, GST on fee
    reference: str


class Refund(_Record):
    refund_id: str
    payment_id: Optional[str]        # may be absent -> "orphan" refund
    amount: int                      # paise
    status: RefundStatus = RefundStatus.PROCESSED
    created_at: datetime
    reference: str


class Settlement(_Record):
    settlement_id: str
    merchant_id: str
    payment_ids: list[str] = Field(default_factory=list)
    gross_amount: int                # paise, sum of captured payments
    fees: int                        # paise
    tax: int                         # paise
    refund_adjustments: int          # paise, positive number that is deducted
    other_adjustments: int = 0       # paise, signed
    net_amount: int                  # paise, as reported by the provider
    settlement_date: datetime
    status: SettlementStatus = SettlementStatus.PROCESSED
    reference: str


class BankEntry(_Record):
    bank_entry_id: str
    amount: int                      # paise, always positive
    credit_or_debit: CreditDebit = CreditDebit.CREDIT
    date: datetime
    reference: str
    description: str = ""


class ExceptionType(str, Enum):
    MISSING_SETTLEMENT = "EXCEPTION_MISSING_SETTLEMENT"
    DUPLICATE_RECORD = "EXCEPTION_DUPLICATE_RECORD"
    FEE_MISMATCH = "EXCEPTION_FEE_MISMATCH"
    UNEXPLAINED_SHORTFALL = "EXCEPTION_UNEXPLAINED_SHORTFALL"
    DELAYED_SETTLEMENT = "EXCEPTION_DELAYED_SETTLEMENT"
    BANK_MISMATCH = "EXCEPTION_BANK_MISMATCH"
    AMBIGUOUS_REFERENCE = "EXCEPTION_AMBIGUOUS_REFERENCE"


class ExceptionStatus(str, Enum):
    OPEN = "OPEN"
    AUTO_RESOLVED = "AUTO_RESOLVED"
    RECOMMEND_REVIEW = "RECOMMEND_REVIEW"
    HUMAN_REVIEW_REQUIRED = "HUMAN_REVIEW_REQUIRED"
    UNRESOLVED = "UNRESOLVED"


class Risk(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ReconciliationException(_Record):
    exception_id: str
    lifecycle_id: str
    transaction_ids: list[str]
    exception_type: ExceptionType
    detected_by: str                 # "deterministic_matcher" | "shadow_ledger"
    expected_value: Optional[int]    # paise
    observed_value: Optional[int]    # paise
    difference: Optional[int]        # paise, expected - observed
    evidence: dict = Field(default_factory=dict)
    status: ExceptionStatus = ExceptionStatus.OPEN
    risk: Risk = Risk.MEDIUM
    resolution: Optional[str] = None
    human_review_required: bool = True


class Batch(_Record):
    """Everything the controller is allowed to see. Ground truth is NOT here."""
    orders: list[Order] = Field(default_factory=list)
    payments: list[Payment] = Field(default_factory=list)
    refunds: list[Refund] = Field(default_factory=list)
    settlements: list[Settlement] = Field(default_factory=list)
    bank_entries: list[BankEntry] = Field(default_factory=list)
