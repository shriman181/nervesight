"""
analytics/cashflow.py
──────────────────────
30 / 60 / 90-day cash flow projection from Tally ledger entries.

Computes:
  - Historical inflows (receipts) and outflows (payments) by period
  - Projected inflows from outstanding receivables (weighted by payment probability)
  - Projected outflows from outstanding payables
  - Net cash position at 30 / 60 / 90 days
  - Working capital stress flag

Input : parsed data dict from TallyConnector.fetch_all()
Output: CashFlowReport dataclass
"""

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

TODAY = date.today()


@dataclass
class CashFlowPeriod:
    label: str                      # "0–30 days", "31–60 days", "61–90 days"
    projected_inflow: float = 0.0   # receivables expected to convert
    projected_outflow: float = 0.0  # payables due in this window
    net: float = 0.0
    confidence: str = "High"        # High / Medium / Low


@dataclass
class CashFlowReport:
    as_of_date: date = field(default_factory=lambda: TODAY)

    # Historical (actual receipts vs payments, last 90 days)
    actual_inflows_90d: float = 0.0
    actual_outflows_90d: float = 0.0
    actual_net_90d: float = 0.0

    # Projected windows
    period_0_30: CashFlowPeriod = field(default_factory=lambda: CashFlowPeriod("0–30 days"))
    period_31_60: CashFlowPeriod = field(default_factory=lambda: CashFlowPeriod("31–60 days"))
    period_61_90: CashFlowPeriod = field(default_factory=lambda: CashFlowPeriod("61–90 days"))

    # Summary
    total_projected_inflow_90d: float = 0.0
    total_projected_outflow_90d: float = 0.0
    net_90d: float = 0.0

    # Working capital
    total_receivables: float = 0.0
    total_payables: float = 0.0
    working_capital: float = 0.0
    working_capital_trend: str = "Stable"   # Improving / Stable / Stressed

    # Stress flags
    stress_flags: list = field(default_factory=list)

    # Chart-ready data
    periods: list = field(default_factory=list)  # list of CashFlowPeriod


def compute_cashflow(data: dict) -> CashFlowReport:
    """
    Projects 30/60/90 day cash flow from:
      - Overdue + upcoming receivables (invoices with due dates)
      - Upcoming payables (purchase vouchers not yet paid)
      - Historical receipt/payment velocity as a calibration factor
    """
    customers = data["customers"]
    vouchers = data["vouchers"]
    report = CashFlowReport()

    cutoff_90d = TODAY - timedelta(days=90)

    # ── Step 1: Historical actuals (last 90 days) ─────────────
    for v in vouchers:
        if v.date < cutoff_90d:
            continue
        if v.voucher_type == "Receipt":
            report.actual_inflows_90d += v.amount
        elif v.voucher_type == "Payment":
            report.actual_outflows_90d += v.amount

    report.actual_net_90d = report.actual_inflows_90d - report.actual_outflows_90d

    # ── Step 2: Payment probability per customer ───────────────
    # Based on whether they've paid before and how overdue they are
    receipt_parties = set(
        v.party_name for v in vouchers
        if v.voucher_type == "Receipt" and v.date >= cutoff_90d
    )

    pay_prob = {}  # party → probability they'll pay in next 90 days
    for name, cust in customers.items():
        has_paid = name in receipt_parties
        bal = cust.closing_balance
        if bal <= 0:
            pay_prob[name] = 0.0
        elif has_paid:
            pay_prob[name] = 0.75   # paid recently → reasonable confidence
        else:
            pay_prob[name] = 0.25   # never paid → low confidence

    # ── Step 3: Project inflows from overdue sales invoices ───
    window_30 = TODAY + timedelta(days=30)
    window_60 = TODAY + timedelta(days=60)
    window_90 = TODAY + timedelta(days=90)

    for v in vouchers:
        if v.voucher_type != "Sales":
            continue

        due = v.due_date
        if due is None:
            # Estimate due date from credit period
            cust = customers.get(v.party_name)
            credit = cust.credit_period_days if cust else 30
            due = v.date + timedelta(days=credit)

        prob = pay_prob.get(v.party_name, 0.4)
        expected = v.amount * prob

        if due <= window_30:
            # Already due or due in 30 days
            report.period_0_30.projected_inflow += expected
        elif due <= window_60:
            report.period_31_60.projected_inflow += expected
        elif due <= window_90:
            report.period_61_90.projected_inflow += expected

    # ── Step 4: Project outflows from purchase vouchers ────────
    # Use avg monthly purchases as proxy for upcoming payables
    purchase_90d = sum(
        v.amount for v in vouchers
        if v.voucher_type == "Purchase" and v.date >= cutoff_90d
    )
    monthly_purchases = purchase_90d / 3.0

    # Spread evenly across windows (simple proxy)
    report.period_0_30.projected_outflow = monthly_purchases
    report.period_31_60.projected_outflow = monthly_purchases
    report.period_61_90.projected_outflow = monthly_purchases

    # ── Step 5: Net per period ─────────────────────────────────
    for period in [report.period_0_30, report.period_31_60, report.period_61_90]:
        period.net = period.projected_inflow - period.projected_outflow
        if period.projected_inflow < period.projected_outflow * 0.5:
            period.confidence = "Low"
        elif period.projected_inflow < period.projected_outflow:
            period.confidence = "Medium"

    # ── Step 6: Totals ─────────────────────────────────────────
    report.total_projected_inflow_90d = (
        report.period_0_30.projected_inflow
        + report.period_31_60.projected_inflow
        + report.period_61_90.projected_inflow
    )
    report.total_projected_outflow_90d = (
        report.period_0_30.projected_outflow
        + report.period_31_60.projected_outflow
        + report.period_61_90.projected_outflow
    )
    report.net_90d = report.total_projected_inflow_90d - report.total_projected_outflow_90d

    # ── Step 7: Working capital ────────────────────────────────
    report.total_receivables = sum(c.closing_balance for c in customers.values())

    # Payables proxy: outstanding purchase ledger (not available in sample,
    # so use 60-day rolling purchases as proxy)
    report.total_payables = monthly_purchases * 1.5

    report.working_capital = report.total_receivables - report.total_payables

    if report.net_90d > 0 and report.actual_net_90d > 0:
        report.working_capital_trend = "Improving"
    elif report.net_90d < 0:
        report.working_capital_trend = "Stressed"
    else:
        report.working_capital_trend = "Stable"

    # ── Step 8: Stress flags ───────────────────────────────────
    if report.period_0_30.net < 0:
        report.stress_flags.append(
            f"⚠ Negative cash flow projected in next 30 days "
            f"(₹{abs(report.period_0_30.net):,.0f} shortfall). "
            f"Accelerate collections or defer purchases."
        )

    if report.total_receivables > report.total_payables * 3:
        report.stress_flags.append(
            f"ℹ High receivables-to-payables ratio "
            f"({report.total_receivables/max(report.total_payables,1):.1f}×) — "
            f"business is effectively financing its customers."
        )

    low_confidence_periods = [
        p for p in [report.period_0_30, report.period_31_60, report.period_61_90]
        if p.confidence == "Low"
    ]
    if low_confidence_periods:
        report.stress_flags.append(
            f"⚠ Low collection confidence in {len(low_confidence_periods)} period(s) — "
            f"several customers have no payment history."
        )

    report.periods = [report.period_0_30, report.period_31_60, report.period_61_90]
    return report


def cashflow_chart_data(report: CashFlowReport) -> dict:
    """Returns chart-ready dict for Streamlit bar_chart."""
    return {
        "Period": [p.label for p in report.periods],
        "Projected Inflow (₹)": [p.projected_inflow for p in report.periods],
        "Projected Outflow (₹)": [p.projected_outflow for p in report.periods],
        "Net (₹)": [p.net for p in report.periods],
    }


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
    report = compute_cashflow(data)

    print(f"\nCash Flow Projection — as of {report.as_of_date}")
    print(f"  Historical net (90d): ₹{report.actual_net_90d:,.0f}")
    print(f"\n  Projected windows:")
    for p in report.periods:
        print(f"    {p.label:<15} In: ₹{p.projected_inflow:>10,.0f}  Out: ₹{p.projected_outflow:>10,.0f}  Net: ₹{p.net:>10,.0f}  [{p.confidence}]")
    print(f"\n  90-day net: ₹{report.net_90d:,.0f}")
    print(f"  Working capital: ₹{report.working_capital:,.0f} ({report.working_capital_trend})")
    print(f"\n  Stress flags:")
    for f in report.stress_flags:
        print(f"    {f}")
