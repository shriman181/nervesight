"""
analytics/receivables.py
────────────────────────
Module C — Receivables Health & Cash Flow

Computes:
  - Receivables aging buckets (0-30 / 31-60 / 61-90 / 90+ days overdue)
  - Total overdue amount
  - Top 5 overdue customers (actionable list)
  - Collections efficiency (trailing 90 days)
  - Cash conversion cycle proxy
  - Per-invoice overdue status

Input : parsed data dict from TallyConnector.fetch_all()
Output: ReceivablesReport dataclass
"""

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

TODAY = date.today()


# ─────────────────────────────────────────
# Output models
# ─────────────────────────────────────────

@dataclass
class OverdueInvoice:
    voucher_number: str
    party_name: str
    invoice_date: date
    due_date: date
    invoice_amount: float
    days_overdue: int
    bucket: str                 # "0-30" / "31-60" / "61-90" / "90+"
    estimated_outstanding: float = 0.0


@dataclass
class PartyOverdue:
    party_name: str
    city: str = ""
    total_outstanding: float = 0.0
    invoices_overdue: int = 0
    oldest_overdue_days: int = 0
    has_ever_paid: bool = False
    risk_level: str = "Medium"  # Low / Medium / High / Critical


@dataclass
class ReceivablesReport:
    as_of_date: date = field(default_factory=lambda: TODAY)

    # Aging buckets (overdue amounts)
    bucket_0_30: float = 0.0
    bucket_31_60: float = 0.0
    bucket_61_90: float = 0.0
    bucket_90_plus: float = 0.0

    # Totals
    total_outstanding: float = 0.0      # from ledger closing balances
    total_overdue: float = 0.0          # invoices past due date
    total_current: float = 0.0          # not yet due

    # Collections
    receipts_90d: float = 0.0
    invoices_raised_90d: float = 0.0
    collections_efficiency_pct: float = 0.0  # receipts / invoices × 100

    # Party-level overdue
    party_overdue: list = field(default_factory=list)   # list[PartyOverdue]

    # Invoice-level (for drill-down)
    overdue_invoices: list = field(default_factory=list)  # list[OverdueInvoice]

    # Anomaly flags
    anomaly_flags: list = field(default_factory=list)   # list[str]


# ─────────────────────────────────────────
# Main compute function
# ─────────────────────────────────────────

def compute_receivables(data: dict) -> ReceivablesReport:
    """
    Returns a ReceivablesReport with aging buckets, overdue parties,
    and collections efficiency.
    """
    customers = data["customers"]
    vouchers = data["vouchers"]

    report = ReceivablesReport()
    cutoff_90d = TODAY - timedelta(days=90)

    # ── Collect receipts and sales (last 90 days) ──────────
    receipts_by_party: dict[str, list] = {}
    sales_90d: list = []

    for v in vouchers:
        if v.voucher_type == "Receipt" and v.date >= cutoff_90d:
            receipts_by_party.setdefault(v.party_name, []).append(v)
            report.receipts_90d += v.amount

        elif v.voucher_type == "Sales" and v.date >= cutoff_90d:
            report.invoices_raised_90d += v.amount
            sales_90d.append(v)

    # Collections efficiency
    if report.invoices_raised_90d > 0:
        report.collections_efficiency_pct = min(
            (report.receipts_90d / report.invoices_raised_90d) * 100, 100
        )

    # ── Total outstanding from ledger ─────────────────────
    report.total_outstanding = sum(
        c.closing_balance for c in customers.values()
    )

    # ── Invoice-level aging ───────────────────────────────
    # Build receipt total per party for netting
    receipt_totals: dict[str, float] = {
        party: sum(r.amount for r in receipts)
        for party, receipts in receipts_by_party.items()
    }

    party_overdue_map: dict[str, PartyOverdue] = {}

    for v in vouchers:
        if v.voucher_type != "Sales":
            continue
        if v.due_date is None:
            continue

        if v.due_date >= TODAY:
            # Not yet due
            report.total_current += v.amount
            continue

        days_overdue = (TODAY - v.due_date).days
        bucket = _aging_bucket(days_overdue)

        oi = OverdueInvoice(
            voucher_number=v.voucher_number,
            party_name=v.party_name,
            invoice_date=v.date,
            due_date=v.due_date,
            invoice_amount=v.amount,
            days_overdue=days_overdue,
            bucket=bucket,
            estimated_outstanding=v.amount,  # simplified (no partial matching)
        )
        report.overdue_invoices.append(oi)

        # Accumulate into aging buckets
        amt = v.amount
        if bucket == "0-30":
            report.bucket_0_30 += amt
        elif bucket == "31-60":
            report.bucket_31_60 += amt
        elif bucket == "61-90":
            report.bucket_61_90 += amt
        else:
            report.bucket_90_plus += amt

        # Build party-level rollup
        if v.party_name not in party_overdue_map:
            cust = customers.get(v.party_name)
            party_overdue_map[v.party_name] = PartyOverdue(
                party_name=v.party_name,
                city=cust.city if cust else "",
                has_ever_paid=v.party_name in receipt_totals,
            )

        po = party_overdue_map[v.party_name]
        po.total_outstanding += amt
        po.invoices_overdue += 1
        po.oldest_overdue_days = max(po.oldest_overdue_days, days_overdue)

    report.total_overdue = (
        report.bucket_0_30 + report.bucket_31_60
        + report.bucket_61_90 + report.bucket_90_plus
    )

    # ── Risk classification for each party ───────────────
    for po in party_overdue_map.values():
        po.risk_level = _party_risk(po)

    # Sort by outstanding desc, take top overdue parties
    report.party_overdue = sorted(
        party_overdue_map.values(),
        key=lambda x: x.total_outstanding,
        reverse=True,
    )

    # Sort invoices by days overdue desc
    report.overdue_invoices.sort(key=lambda x: x.days_overdue, reverse=True)

    # ── Anomaly flags ─────────────────────────────────────
    report.anomaly_flags = _detect_anomalies(
        customers, vouchers, receipts_by_party, report
    )

    return report


def aging_buckets_dict(report: ReceivablesReport) -> dict:
    """Returns aging data as a dict for charting."""
    return {
        "0–30 days": report.bucket_0_30,
        "31–60 days": report.bucket_31_60,
        "61–90 days": report.bucket_61_90,
        "90+ days": report.bucket_90_plus,
    }


# ─────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────

def _aging_bucket(days_overdue: int) -> str:
    if days_overdue <= 30:
        return "0-30"
    elif days_overdue <= 60:
        return "31-60"
    elif days_overdue <= 90:
        return "61-90"
    else:
        return "90+"


def _party_risk(po: PartyOverdue) -> str:
    if po.oldest_overdue_days > 90 and not po.has_ever_paid:
        return "Critical"
    if po.oldest_overdue_days > 90 or po.total_outstanding > 200000:
        return "High"
    if po.oldest_overdue_days > 60 or po.total_outstanding > 100000:
        return "Medium"
    return "Low"


def _detect_anomalies(customers, vouchers, receipts_by_party, report) -> list[str]:
    flags = []

    # Flag 1: customers with 90+ day outstanding and zero payment history
    for po in report.party_overdue:
        if po.oldest_overdue_days > 90 and not po.has_ever_paid:
            flags.append(
                f"⚠ {po.party_name}: ₹{po.total_outstanding:,.0f} overdue {po.oldest_overdue_days}d "
                f"— no payment ever received. Escalate immediately."
            )

    # Flag 2: collections efficiency below 75%
    if report.collections_efficiency_pct < 75:
        flags.append(
            f"⚠ Collections efficiency at {report.collections_efficiency_pct:.1f}% "
            f"(last 90 days) — cash flow under stress."
        )

    # Flag 3: 90+ bucket > 30% of total overdue
    if report.total_overdue > 0:
        chronic_pct = (report.bucket_90_plus / report.total_overdue) * 100
        if chronic_pct > 30:
            flags.append(
                f"⚠ {chronic_pct:.0f}% of overdue receivables are 90+ days old "
                f"(₹{report.bucket_90_plus:,.0f}) — structural collections problem."
            )

    # Flag 4: large round-number invoice check (audit signal)
    round_invoices = [
        v for v in vouchers
        if v.voucher_type == "Sales" and v.amount % 10000 == 0 and v.amount >= 50000
    ]
    if len(round_invoices) >= 3:
        flags.append(
            f"ℹ {len(round_invoices)} invoices are exact round numbers ≥ ₹50,000 "
            f"— review for manual overrides."
        )

    return flags


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
    report = compute_receivables(data)

    print(f"\nReceivables Report — as of {report.as_of_date}")
    print(f"  Total Outstanding : ₹{report.total_outstanding:,.0f}")
    print(f"  Total Overdue     : ₹{report.total_overdue:,.0f}")
    print(f"  Collections Eff.  : {report.collections_efficiency_pct:.1f}%")
    print(f"\n  Aging Buckets:")
    print(f"    0–30 days  : ₹{report.bucket_0_30:,.0f}")
    print(f"    31–60 days : ₹{report.bucket_31_60:,.0f}")
    print(f"    61–90 days : ₹{report.bucket_61_90:,.0f}")
    print(f"    90+ days   : ₹{report.bucket_90_plus:,.0f}")
    print(f"\n  Top Overdue Parties:")
    for po in report.party_overdue[:5]:
        print(f"    {po.party_name:<35} ₹{po.total_outstanding:>10,.0f}  [{po.risk_level}]  Paid before: {po.has_ever_paid}")
    print(f"\n  Anomaly Flags:")
    for flag in report.anomaly_flags:
        print(f"    {flag}")
