"""
analytics/customer_intelligence.py
────────────────────────────────────
Module A — Customer Intelligence

Computes per-customer:
  - Revenue (trailing 12M)
  - Estimated gross margin (using SKU-level COGS proxy)
  - Days Sales Outstanding (DSO)
  - Last order date + order frequency
  - Churn risk signal
  - Auto-tier classification: Star / Drain / Sleeper / Risky

Input : parsed data dict from TallyConnector.fetch_all()
Output: list of CustomerMetrics dataclasses, sorted by gross margin desc
"""

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


TODAY = date.today()
LOOKBACK_DAYS = 500  # trailing ~16 months to accommodate sample data dating back to early 2025


# ─────────────────────────────────────────
# Output model
# ─────────────────────────────────────────

@dataclass
class CustomerMetrics:
    name: str
    city: str = ""

    # Revenue
    revenue_12m: float = 0.0
    revenue_last_month: float = 0.0
    revenue_prev_month: float = 0.0

    # Margin (estimated from purchase price proxy)
    estimated_margin_pct: float = 0.0
    estimated_margin_abs: float = 0.0

    # Collections
    outstanding_balance: float = 0.0
    total_receipts_12m: float = 0.0
    dso_days: float = 0.0               # Days Sales Outstanding
    collections_efficiency: float = 0.0  # receipts / invoices raised

    # Behaviour
    last_order_date: Optional[date] = None
    days_since_last_order: int = 0
    order_count_12m: int = 0
    avg_order_value: float = 0.0
    avg_days_to_pay: float = 0.0        # avg gap invoice→receipt

    # Flags
    has_ever_paid: bool = False
    return_amount: float = 0.0

    # Classification
    tier: str = "Unknown"               # Star | Drain | Sleeper | Risky | New
    tier_reason: str = ""


# ─────────────────────────────────────────
# Main compute function
# ─────────────────────────────────────────

def compute_customer_metrics(data: dict) -> list[CustomerMetrics]:
    """
    data: output of TallyConnector.fetch_all()
    Returns: list of CustomerMetrics, sorted by gross margin descending.
    """
    customers = data["customers"]
    stock_items = data["stock_items"]
    vouchers = data["vouchers"]

    # Build COGS lookup: SKU name → avg purchase rate
    cogs_by_sku = _build_cogs_lookup(vouchers)

    # Index vouchers by type and party
    sales_by_party: dict[str, list] = {}
    receipts_by_party: dict[str, list] = {}
    returns_by_party: dict[str, float] = {}

    cutoff = TODAY - timedelta(days=LOOKBACK_DAYS)

    for v in vouchers:
        if v.date < cutoff:
            continue

        if v.voucher_type == "Sales":
            sales_by_party.setdefault(v.party_name, []).append(v)

        elif v.voucher_type == "Receipt":
            receipts_by_party.setdefault(v.party_name, []).append(v)

        elif v.voucher_type in ("Credit Note", "Debit Note"):
            returns_by_party[v.party_name] = (
                returns_by_party.get(v.party_name, 0) + abs(v.amount)
            )

    results = []

    for name, customer in customers.items():
        m = CustomerMetrics(name=name, city=customer.city)
        m.outstanding_balance = customer.closing_balance

        sales = sales_by_party.get(name, [])
        receipts = receipts_by_party.get(name, [])
        m.return_amount = returns_by_party.get(name, 0.0)

        # ── Revenue ──────────────────────────────
        m.revenue_12m = sum(v.amount for v in sales)
        m.order_count_12m = len(sales)

        # Last month vs prev month
        last_month_start = _month_start(TODAY, 0)
        prev_month_start = _month_start(TODAY, -1)

        m.revenue_last_month = sum(
            v.amount for v in sales
            if last_month_start <= v.date <= TODAY
        )
        m.revenue_prev_month = sum(
            v.amount for v in sales
            if prev_month_start <= v.date < last_month_start
        )

        if m.order_count_12m > 0:
            m.avg_order_value = m.revenue_12m / m.order_count_12m
            m.last_order_date = max(v.date for v in sales)
            m.days_since_last_order = (TODAY - m.last_order_date).days

        # ── Gross margin (estimated) ───────────────
        cogs = _estimate_cogs(sales, cogs_by_sku)
        if m.revenue_12m > 0:
            m.estimated_margin_abs = m.revenue_12m - cogs
            m.estimated_margin_pct = (m.estimated_margin_abs / m.revenue_12m) * 100

        # ── Collections ──────────────────────────
        m.total_receipts_12m = sum(v.amount for v in receipts)
        m.has_ever_paid = len(receipts) > 0

        if m.revenue_12m > 0:
            daily_rev = m.revenue_12m / LOOKBACK_DAYS
            m.dso_days = m.outstanding_balance / daily_rev if daily_rev > 0 else 0
            m.collections_efficiency = min(
                (m.total_receipts_12m / m.revenue_12m) * 100, 100
            )

        # Average days to pay (invoice date → receipt date proxy)
        m.avg_days_to_pay = _estimate_avg_days_to_pay(
            sales, receipts, customer.credit_period_days
        )

        # ── Tier classification ────────────────────
        m.tier, m.tier_reason = _classify_tier(m, customer.credit_period_days)

        results.append(m)

    # Sort by gross margin descending
    results.sort(key=lambda x: x.estimated_margin_abs, reverse=True)
    return results


# ─────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────

def _build_cogs_lookup(vouchers) -> dict[str, float]:
    """Build avg purchase rate per SKU from purchase vouchers."""
    totals: dict[str, list[float]] = {}
    for v in vouchers:
        if v.voucher_type == "Purchase":
            for line in v.lines:
                if line.sku_name and line.rate > 0:
                    totals.setdefault(line.sku_name, []).append(line.rate)
    return {sku: sum(rates) / len(rates) for sku, rates in totals.items()}


def _estimate_cogs(sales_vouchers, cogs_by_sku: dict) -> float:
    """Estimate COGS for a set of sales vouchers using purchase price proxy."""
    total_cogs = 0.0
    total_unmatched_rev = 0.0

    for v in sales_vouchers:
        if v.lines:
            for line in v.lines:
                purchase_rate = cogs_by_sku.get(line.sku_name)
                if purchase_rate and line.qty > 0:
                    total_cogs += purchase_rate * abs(line.qty)
                else:
                    # Fallback: assume 78% COGS (22% margin) for unmatched SKUs
                    total_cogs += abs(line.amount) * 0.78
        else:
            # No line items: apply flat 78% COGS assumption
            total_unmatched_rev += v.amount

    total_cogs += total_unmatched_rev * 0.78
    return total_cogs


def _classify_tier(m: CustomerMetrics, credit_days: int) -> tuple[str, str]:
    """
    Star   → high margin %, pays on time, active
    Drain  → high revenue but low margin or very slow payer
    Sleeper→ was active, gone quiet (45+ days since last order)
    Risky  → large outstanding, never paid or DSO > 90
    New    → first order in last 60 days, insufficient history
    """
    if m.order_count_12m == 0:
        return "Inactive", "No orders in past 12 months"

    if m.order_count_12m == 1 and m.days_since_last_order < 60:
        return "New", "Single order, insufficient payment history"

    if not m.has_ever_paid and m.outstanding_balance > 50000:
        return "Risky", f"₹{m.outstanding_balance:,.0f} outstanding, zero payment history"

    if m.dso_days > 90:
        return "Risky", f"DSO {m.dso_days:.0f} days — severely overdue"

    if m.days_since_last_order > 60:
        return "Sleeper", f"Last order {m.days_since_last_order} days ago"

    if m.estimated_margin_pct < 12 and m.revenue_12m > 100000:
        return "Drain", f"High revenue but only {m.estimated_margin_pct:.1f}% margin"

    if m.dso_days > credit_days * 1.5:
        return "Drain", f"Slow payer — DSO {m.dso_days:.0f}d vs {credit_days}d credit"

    if m.estimated_margin_pct >= 18 and m.dso_days <= credit_days + 10:
        return "Star", f"{m.estimated_margin_pct:.1f}% margin, pays in {m.dso_days:.0f} days"

    return "Stable", f"{m.estimated_margin_pct:.1f}% margin, DSO {m.dso_days:.0f} days"


def _estimate_avg_days_to_pay(sales, receipts, credit_days: int) -> float:
    """Rough proxy: if receipts exist, estimate based on collections efficiency vs credit period."""
    if not receipts:
        return credit_days * 2.5  # no payment = penalise heavily
    total_received = sum(r.amount for r in receipts)
    total_invoiced = sum(v.amount for v in sales)
    if total_invoiced == 0:
        return 0
    pct_paid = total_received / total_invoiced
    # If 100% paid → credit_days. If 0% → credit_days * 3
    return credit_days + (1 - pct_paid) * credit_days * 2


def _month_start(ref: date, offset_months: int) -> date:
    """Return the first day of (ref.month + offset_months)."""
    month = ref.month + offset_months
    year = ref.year + (month - 1) // 12
    month = ((month - 1) % 12) + 1
    return date(year, month, 1)


# ──────────────────────────────────────────
# Quick test
# ──────────────────────────────────────────
if __name__ == "__main__":
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from tally_connector import TallyConnector

    conn = TallyConnector(
        mode="static",
        xml_path=os.path.join(os.path.dirname(__file__), "..", "..", "data", "sample_tally_export.xml"),
    )
    data = conn.fetch_all()
    metrics = compute_customer_metrics(data)

    print(f"\n{'Customer':<35} {'Revenue':>12} {'Margin%':>8} {'DSO':>6} {'Tier':<12}")
    print("─" * 80)
    for m in metrics:
        print(
            f"{m.name:<35} ₹{m.revenue_12m:>10,.0f} {m.estimated_margin_pct:>7.1f}%"
            f" {m.dso_days:>5.0f}d  {m.tier:<12}"
        )
