"""
analytics/inventory.py
──────────────────────
Module B — SKU & Segment Health

Computes per-SKU:
  - Gross margin % (sale price vs purchase price)
  - Sales velocity (units/month, 3-month avg)
  - Dead stock flag (no movement in 60+ days with stock on hand)
  - Return rate
  - Quadrant classification: Star / Volume Trap / Hidden Gem / Dead Weight

Input : parsed data dict from TallyConnector.fetch_all()
Output: list of SKUMetrics dataclasses
"""

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

TODAY = date.today()
DEAD_STOCK_DAYS = 60     # no movement in 60 days = dead stock
VELOCITY_WINDOW = 90     # 3-month window for velocity calc


@dataclass
class SKUMetrics:
    name: str
    category: str = ""
    unit: str = "Piece"

    # Stock position
    stock_on_hand: float = 0.0
    stock_value: float = 0.0            # stock_on_hand × avg purchase rate

    # Pricing
    avg_sale_rate: float = 0.0
    avg_purchase_rate: float = 0.0
    gross_margin_pct: float = 0.0
    gross_margin_abs_per_unit: float = 0.0

    # Velocity
    units_sold_90d: float = 0.0
    units_sold_monthly_avg: float = 0.0  # over last 90 days
    velocity_trend: str = "Stable"       # Growing / Declining / Stable / New

    # Returns
    units_returned: float = 0.0
    return_rate_pct: float = 0.0

    # Flags
    is_dead_stock: bool = False
    days_since_last_sale: int = 0
    last_sale_date: Optional[date] = None

    # Classification
    quadrant: str = "Unknown"
    quadrant_reason: str = ""


def compute_sku_metrics(data: dict) -> list[SKUMetrics]:
    """
    Returns list of SKUMetrics sorted by gross_margin_abs_per_unit × velocity desc.
    """
    stock_items = data["stock_items"]
    vouchers = data["vouchers"]

    # Build purchase rate lookup (avg per SKU from purchase vouchers)
    purchase_rates: dict[str, list[float]] = {}
    for v in vouchers:
        if v.voucher_type == "Purchase":
            for line in v.lines:
                if line.sku_name and line.rate > 0:
                    purchase_rates.setdefault(line.sku_name, []).append(line.rate)

    avg_purchase_rate: dict[str, float] = {
        sku: sum(rates) / len(rates) for sku, rates in purchase_rates.items()
    }

    # Sales lines per SKU (last 90 days and full year)
    cutoff_90d = TODAY - timedelta(days=VELOCITY_WINDOW)
    cutoff_180d = TODAY - timedelta(days=180)

    sales_qty_90d: dict[str, float] = {}
    sales_qty_180d: dict[str, float] = {}
    sales_rates: dict[str, list[float]] = {}
    last_sale: dict[str, date] = {}
    return_qty: dict[str, float] = {}

    for v in vouchers:
        if v.voucher_type == "Sales" and v.date >= cutoff_180d:
            for line in v.lines:
                sku = line.sku_name
                if not sku:
                    continue
                qty = abs(line.qty)
                if v.date >= cutoff_90d:
                    sales_qty_90d[sku] = sales_qty_90d.get(sku, 0) + qty
                sales_qty_180d[sku] = sales_qty_180d.get(sku, 0) + qty
                if line.rate > 0:
                    sales_rates.setdefault(sku, []).append(line.rate)
                if sku not in last_sale or v.date > last_sale[sku]:
                    last_sale[sku] = v.date

        elif v.voucher_type in ("Credit Note", "Debit Note") and v.date >= cutoff_90d:
            for line in v.lines:
                sku = line.sku_name
                if sku:
                    return_qty[sku] = return_qty.get(sku, 0) + abs(line.qty)

    results = []

    # Process every SKU in the master (whether or not it sold recently)
    all_skus = set(stock_items.keys()) | set(sales_qty_90d.keys())

    for sku_name in all_skus:
        m = SKUMetrics(name=sku_name)

        item = stock_items.get(sku_name)
        if item:
            m.category = item.category
            m.unit = item.unit
            m.stock_on_hand = item.closing_qty

        # Purchase rate
        prate = avg_purchase_rate.get(sku_name) or (item.closing_rate if item else 0)
        m.avg_purchase_rate = prate
        m.stock_value = m.stock_on_hand * prate

        # Sale rate
        srates = sales_rates.get(sku_name, [])
        m.avg_sale_rate = sum(srates) / len(srates) if srates else 0

        # Margin
        if m.avg_sale_rate > 0 and m.avg_purchase_rate > 0:
            m.gross_margin_abs_per_unit = m.avg_sale_rate - m.avg_purchase_rate
            m.gross_margin_pct = (m.gross_margin_abs_per_unit / m.avg_sale_rate) * 100

        # Velocity
        m.units_sold_90d = sales_qty_90d.get(sku_name, 0)
        m.units_sold_monthly_avg = m.units_sold_90d / 3.0

        # Velocity trend: compare 90d to prior 90d
        prior_90d = sales_qty_180d.get(sku_name, 0) - m.units_sold_90d
        if m.units_sold_90d == 0 and prior_90d == 0:
            m.velocity_trend = "No Sales"
        elif prior_90d == 0:
            m.velocity_trend = "New"
        elif m.units_sold_90d > prior_90d * 1.15:
            m.velocity_trend = "Growing"
        elif m.units_sold_90d < prior_90d * 0.85:
            m.velocity_trend = "Declining"
        else:
            m.velocity_trend = "Stable"

        # Returns
        m.units_returned = return_qty.get(sku_name, 0)
        if m.units_sold_90d > 0:
            m.return_rate_pct = (m.units_returned / m.units_sold_90d) * 100

        # Dead stock
        m.last_sale_date = last_sale.get(sku_name)
        if m.last_sale_date:
            m.days_since_last_sale = (TODAY - m.last_sale_date).days
        else:
            m.days_since_last_sale = 999

        m.is_dead_stock = (
            m.stock_on_hand > 0
            and m.days_since_last_sale >= DEAD_STOCK_DAYS
        )

        # Quadrant classification
        m.quadrant, m.quadrant_reason = _classify_quadrant(m)

        results.append(m)

    # Sort: Stars first, then by margin × velocity score
    results.sort(
        key=lambda x: (
            x.quadrant == "Dead Weight",
            -(x.gross_margin_pct * x.units_sold_monthly_avg)
        )
    )
    return results


def _classify_quadrant(m: SKUMetrics) -> tuple[str, str]:
    """
    Star         → high margin + good velocity
    Volume Trap  → high velocity + low margin (you're busy but not profitable)
    Hidden Gem   → high margin + low velocity (undermarketed)
    Dead Weight  → low margin + low/no velocity + stock sitting
    """
    high_margin = m.gross_margin_pct >= 16
    good_velocity = m.units_sold_monthly_avg >= 10

    if m.is_dead_stock:
        return "Dead Weight", f"No sales in {m.days_since_last_sale}d, {m.stock_on_hand:.0f} units sitting"

    if m.units_sold_90d == 0 and m.stock_on_hand == 0:
        return "Inactive", "No stock, no recent sales"

    if high_margin and good_velocity:
        return "Star", f"{m.gross_margin_pct:.1f}% margin, {m.units_sold_monthly_avg:.0f} units/mo"

    if high_margin and not good_velocity:
        return "Hidden Gem", f"{m.gross_margin_pct:.1f}% margin but only {m.units_sold_monthly_avg:.1f} units/mo — push harder"

    if not high_margin and good_velocity:
        return "Volume Trap", f"Moving {m.units_sold_monthly_avg:.0f} units/mo but only {m.gross_margin_pct:.1f}% margin"

    if m.return_rate_pct > 10:
        return "Problem SKU", f"{m.return_rate_pct:.1f}% return rate — quality issue"

    return "Average", f"{m.gross_margin_pct:.1f}% margin, {m.units_sold_monthly_avg:.1f} units/mo"


def get_dead_stock_summary(metrics: list[SKUMetrics]) -> dict:
    dead = [m for m in metrics if m.is_dead_stock]
    return {
        "count": len(dead),
        "total_value": sum(m.stock_value for m in dead),
        "items": dead,
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
    metrics = compute_sku_metrics(data)

    print(f"\n{'SKU':<40} {'Margin%':>8} {'Vel/mo':>8} {'Quadrant':<16} Dead?")
    print("─" * 90)
    for m in metrics:
        print(
            f"{m.name:<40} {m.gross_margin_pct:>7.1f}%"
            f" {m.units_sold_monthly_avg:>7.1f}  {m.quadrant:<16} {'⚠' if m.is_dead_stock else ''}"
        )
