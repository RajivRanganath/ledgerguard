"""Money handling for LedgerGuard.

Every monetary value in this system is an ``int`` count of minor units
(paise for INR). Binary floating point is never used for money. Decimal is
used only transiently inside rate calculations, and the result is always
quantised back to an integer number of paise with ROUND_HALF_UP.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

Paise = int

# Simplified Razorpay-like fee model. Documented in docs/limitations.md.
PLATFORM_FEE_RATE = Decimal("0.02")   # 2% of the captured amount
GST_RATE = Decimal("0.18")            # 18% GST charged on the platform fee


def to_paise(rupees: str | int | Decimal) -> Paise:
    """Convert a rupee amount to integer paise (ROUND_HALF_UP)."""
    d = Decimal(str(rupees))
    return int((d * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def to_rupees_str(paise: Paise) -> str:
    """Render integer paise as a fixed 2-decimal rupee string."""
    sign = "-" if paise < 0 else ""
    q = abs(int(paise))
    return f"{sign}{q // 100}.{q % 100:02d}"


def format_inr(paise: Paise) -> str:
    return f"INR {to_rupees_str(paise)}"


def apply_rate(base_paise: Paise, rate: Decimal) -> Paise:
    """Apply a rate to a paise amount, returning integer paise.

    ROUND_HALF_UP is applied once, at the paise boundary. This is the only
    place rounding of a derived money value is allowed to happen.
    """
    if not isinstance(base_paise, int):
        raise TypeError(f"money must be int paise, got {type(base_paise).__name__}")
    return int((Decimal(base_paise) * rate).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def platform_fee(captured_paise: Paise) -> Paise:
    """Platform fee on a captured payment."""
    return apply_rate(captured_paise, PLATFORM_FEE_RATE)


def gst_on_fee(fee_paise: Paise) -> Paise:
    """GST charged on the platform fee."""
    return apply_rate(fee_paise, GST_RATE)
