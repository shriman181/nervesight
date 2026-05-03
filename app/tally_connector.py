"""
tally_connector.py
──────────────────
Pulls data from Tally in two modes:
  1. STATIC  — parse an exported XML file (dev / demo mode)
  2. LIVE    — query Tally Prime's local HTTP server on port 9000

Tally Prime exposes an HTTP endpoint at http://localhost:9000
We POST an XML request envelope and parse the XML response.

Usage:
    connector = TallyConnector(mode="static", xml_path="data/sample_tally_export.xml")
    data = connector.fetch_all()

    connector = TallyConnector(mode="live", tally_host="localhost", tally_port=9000)
    data = connector.fetch_all()
"""

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Optional
import os

try:
    import requests
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False


# ─────────────────────────────────────────
# Data models
# ─────────────────────────────────────────

@dataclass
class Customer:
    name: str
    city: str = ""
    state: str = ""
    pincode: str = ""
    credit_period_days: int = 30
    closing_balance: float = 0.0        # outstanding amount owed TO us


@dataclass
class StockItem:
    name: str
    category: str = ""
    closing_qty: float = 0.0
    closing_rate: float = 0.0           # avg purchase price (COGS proxy)
    opening_qty: float = 0.0
    opening_rate: float = 0.0
    unit: str = "Piece"


@dataclass
class VoucherLine:
    sku_name: str
    qty: float
    rate: float
    amount: float


@dataclass
class Voucher:
    voucher_type: str                   # Sales / Purchase / Receipt / Credit Note
    date: date
    voucher_number: str
    party_name: str
    amount: float
    due_date: Optional[date] = None
    narration: str = ""
    lines: list = field(default_factory=list)


# ─────────────────────────────────────────
# XML request templates for live mode
# ─────────────────────────────────────────

TALLY_REQUEST_LEDGER = """
<ENVELOPE>
  <HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST><TYPE>Collection</TYPE><ID>All Ledgers</ID></HEADER>
  <BODY>
    <DESC>
      <STATICVARIABLES><SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT></STATICVARIABLES>
      <TDL>
        <TDLMESSAGE>
          <COLLECTION NAME="All Ledgers" ISMODIFY="No">
            <TYPE>Ledger</TYPE>
            <FETCH>Name,Parent,ClosingBalance,CreditPeriod,LedgerCity,LedgerState,PinCode</FETCH>
          </COLLECTION>
        </TDLMESSAGE>
      </TDL>
    </DESC>
  </BODY>
</ENVELOPE>
"""

TALLY_REQUEST_VOUCHERS = """
<ENVELOPE>
  <HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST><TYPE>Collection</TYPE><ID>Day Book</ID></HEADER>
  <BODY>
    <DESC>
      <STATICVARIABLES>
        <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
        <SVFROMDATE>{from_date}</SVFROMDATE>
        <SVTODATE>{to_date}</SVTODATE>
      </STATICVARIABLES>
    </DESC>
  </BODY>
</ENVELOPE>
"""


# ─────────────────────────────────────────
# Main connector class
# ─────────────────────────────────────────

class TallyConnector:

    def __init__(
        self,
        mode: str = "static",
        xml_path: str = "data/sample_tally_export.xml",
        tally_host: str = "localhost",
        tally_port: int = 9000,
    ):
        """
        mode: "static" | "live"
        """
        self.mode = mode
        self.xml_path = xml_path
        self.tally_url = f"http://{tally_host}:{tally_port}"

        self.customers: dict[str, Customer] = {}
        self.stock_items: dict[str, StockItem] = {}
        self.vouchers: list[Voucher] = []

    # ──────────────────────────────────────
    # Public entry point
    # ──────────────────────────────────────

    def fetch_all(self) -> dict:
        """
        Returns a dict with keys: customers, stock_items, vouchers.
        In static mode, parses the sample XML.
        In live mode, queries Tally's HTTP server.
        """
        if self.mode == "static":
            self._parse_static_xml()
        elif self.mode == "live":
            self._fetch_live()
        else:
            raise ValueError(f"Unknown mode: {self.mode}")

        return {
            "customers": self.customers,
            "stock_items": self.stock_items,
            "vouchers": self.vouchers,
        }

    # ──────────────────────────────────────
    # Static XML parsing
    # ──────────────────────────────────────

    def _parse_static_xml(self):
        """Parse the exported Tally XML file."""
        path = self.xml_path
        if not os.path.exists(path):
            # Try relative to project root
            path = os.path.join(os.path.dirname(__file__), "..", path)

        tree = ET.parse(path)
        root = tree.getroot()

        exportdata = root.find(".//EXPORTDATA")
        if exportdata is None:
            raise ValueError("Invalid Tally XML: <EXPORTDATA> not found.")

        for collection in exportdata.findall("COLLECTION"):
            ctype = collection.get("NAME", "").upper()

            if ctype == "LEDGER":
                self._parse_ledgers(collection)

            elif ctype == "STOCKITEM":
                self._parse_stock_items(collection)

            elif ctype == "VOUCHER":
                # Determine voucher type from first child
                vtype = collection.get("TYPE", "")
                self._parse_vouchers(collection, vtype)

    def _parse_ledgers(self, collection):
        for ledger in collection.findall("LEDGER"):
            name = ledger.get("NAME", "").strip()
            parent = self._text(ledger, "PARENT")

            # Only import customer ledgers (Sundry Debtors)
            if "debtor" not in parent.lower():
                continue

            self.customers[name] = Customer(
                name=name,
                city=self._text(ledger, "CITY"),
                state=self._text(ledger, "STATE"),
                pincode=self._text(ledger, "PINCODE"),
                credit_period_days=int(self._text(ledger, "CREDITPERIOD") or 30),
                closing_balance=float(self._text(ledger, "CLOSINGBALANCE") or 0),
            )

    def _parse_stock_items(self, collection):
        for item in collection.findall("STOCKITEM"):
            name = item.get("NAME", "").strip()
            self.stock_items[name] = StockItem(
                name=name,
                category=self._text(item, "PARENT"),
                closing_qty=float(self._text(item, "CLOSINGBALANCEQTY") or 0),
                closing_rate=float(self._text(item, "CLOSINGRATE") or 0),
                opening_qty=float(self._text(item, "OPENINGBALANCEQTY") or 0),
                opening_rate=float(self._text(item, "OPENINGRATE") or 0),
                unit=self._text(item, "BASEUNITS"),
            )

    def _parse_vouchers(self, collection, voucher_type: str):
        for v in collection.findall("VOUCHER"):
            date_str = self._text(v, "DATE")
            due_str = self._text(v, "DUEDATE")

            voucher = Voucher(
                voucher_type=voucher_type,
                date=self._parse_date(date_str),
                voucher_number=self._text(v, "VOUCHERNUMBER"),
                party_name=self._text(v, "PARTYLEDGERNAME"),
                amount=float(self._text(v, "AMOUNT") or 0),
                due_date=self._parse_date(due_str) if due_str else None,
                narration=self._text(v, "NARRATION"),
            )

            # Parse line items
            for entry in v.findall(".//LEDGERENTRIES"):
                sku = self._text(entry, "STOCKITEMNAME")
                if sku:
                    voucher.lines.append(VoucherLine(
                        sku_name=sku,
                        qty=float(self._text(entry, "BILLEDQTY") or 0),
                        rate=float(self._text(entry, "RATE") or 0),
                        amount=float(self._text(entry, "AMOUNT") or 0),
                    ))

            self.vouchers.append(voucher)

    # ──────────────────────────────────────
    # Live Tally HTTP mode
    # ──────────────────────────────────────

    def _fetch_live(self):
        """Query Tally Prime's local HTTP server."""
        if not REQUESTS_AVAILABLE:
            raise ImportError("Install 'requests' to use live mode: pip install requests")

        try:
            # Test connection
            resp = requests.get(self.tally_url, timeout=3)
        except Exception as e:
            raise ConnectionError(
                f"Cannot reach Tally at {self.tally_url}. "
                f"Make sure Tally Prime is running and HTTP server is enabled. Error: {e}"
            )

        from_date = "20250101"
        to_date = datetime.today().strftime("%Y%m%d")

        # Fetch ledgers
        ledger_xml = self._tally_post(TALLY_REQUEST_LEDGER)
        if ledger_xml:
            root = ET.fromstring(ledger_xml)
            col = root.find(".//COLLECTION")
            if col:
                self._parse_ledgers(col)

        # Fetch vouchers (day book)
        voucher_xml = self._tally_post(
            TALLY_REQUEST_VOUCHERS.format(from_date=from_date, to_date=to_date)
        )
        if voucher_xml:
            root = ET.fromstring(voucher_xml)
            for col in root.findall(".//COLLECTION"):
                vtype = col.get("TYPE", "")
                self._parse_vouchers(col, vtype)

    def _tally_post(self, xml_body: str) -> Optional[str]:
        try:
            resp = requests.post(
                self.tally_url,
                data=xml_body.encode("utf-8"),
                headers={"Content-Type": "application/xml"},
                timeout=10,
            )
            resp.raise_for_status()
            return resp.text
        except Exception as e:
            print(f"[TallyConnector] Live fetch error: {e}")
            return None

    # ──────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────

    @staticmethod
    def _text(element, tag: str) -> str:
        child = element.find(tag)
        return child.text.strip() if child is not None and child.text else ""

    @staticmethod
    def _parse_date(date_str: str) -> Optional[date]:
        """Parse Tally date format YYYYMMDD."""
        if not date_str or len(date_str) < 8:
            return None
        try:
            return datetime.strptime(date_str[:8], "%Y%m%d").date()
        except ValueError:
            return None


# ──────────────────────────────────────────
# Quick test
# ──────────────────────────────────────────
if __name__ == "__main__":
    conn = TallyConnector(
        mode="static",
        xml_path=os.path.join(os.path.dirname(__file__), "..", "data", "sample_tally_export.xml"),
    )
    data = conn.fetch_all()
    print(f"Customers loaded : {len(data['customers'])}")
    print(f"SKUs loaded      : {len(data['stock_items'])}")
    print(f"Vouchers loaded  : {len(data['vouchers'])}")
    for name, c in data["customers"].items():
        print(f"  {name} — Outstanding: ₹{c.closing_balance:,.0f}")
