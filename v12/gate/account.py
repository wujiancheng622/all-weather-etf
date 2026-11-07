"""Parse qmt_gate's strategy view. Amounts are yuan; settled shares belong to v12."""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_HALF_UP

from ..config import CODES
from .order_loop import Stop

D = Decimal
CENT = D("0.01")
QDII = frozenset((CODES["nasdaq"], CODES["sp500"]))


def money(value, name):
    try:
        result = D(str(value))
    except (ValueError, InvalidOperation):
        raise Stop(f"Invalid amount: {name}") from None
    if not result.is_finite() or result < 0:
        raise Stop(f"Invalid amount: {name}")
    return result


def shares(value, name):
    if type(value) is not int or value < 0:
        raise Stop(f"Invalid share quantity: {name}")
    return value


def fee_ceiling(quantity, price):
    fee = max(D(5), quantity * price * D(".00005"))
    return (fee * D("1.1")).quantize(CENT, ROUND_CEILING)


def buy_reserve(quantity, price):
    """Same upward whole-cent unit price and integer-cent fee cushion as shared_execution."""
    gross = price.quantize(CENT, ROUND_CEILING) * quantity
    rule = max(D(5), (gross * D(".00005")).quantize(CENT, ROUND_HALF_UP))
    fee = max(
        fee_ceiling(quantity, price),
        (gross * D(".002")).quantize(CENT, ROUND_CEILING),
        ((rule * 100 * 11 // 10) + 1) / 100,
    )
    return gross + fee if quantity else D(0)


@dataclass
class Account:
    cash: Decimal
    available: Decimal
    reserved: Decimal
    unsettled: Decimal
    receivable: Decimal
    market_value: Decimal
    quantities: dict
    sellable: dict
    blocked: set

    @property
    def nav(self):
        return self.cash + self.market_value + self.receivable

    @classmethod
    def parse(cls, view):
        if (
            view.get("strategy_id") != "v12"
            or view.get("asset_type") != "ETF"
            or view.get("blocked") is not False
            or view.get("broker_account_verified") is not True
        ):
            raise Stop("Expected an unblocked verified v12 ETF strategy view")
        cash, available, reserved, unsettled, receivable, market, economic, sell_fees, buy_fees = (
            money(view.get(key), key)
            for key in (
                "accounting_cash",
                "available_cash",
                "reserved_cash",
                "unsettled_sale_proceeds",
                "dividend_receivable",
                "market_value",
                "economic_equity",
                "reserved_sale_fees",
                "reserved_buy_fees",
            )
        )
        if (
            cash + market != economic
            or reserved < sell_fees + buy_fees
            or available != max(D(0), cash - reserved - unsettled)
        ):
            raise Stop("Inconsistent strategy cash, reserves or equity")
        quantities = dict.fromkeys(CODES.values(), 0)
        sellable = quantities.copy()
        blocked = set()
        seen = set()
        for row in view["positions"]:
            code = row["security"]
            if code not in quantities or code in seen or type(row.get("blocked")) is not bool:
                raise Stop("Unexpected strategy position")
            seen.add(code)
            quantities[code] = shares(row["quantity"], code)
            sellable[code] = shares(row["sellable"], code)
            if sellable[code] > quantities[code]:
                raise Stop("Sellable exceeds owned quantity")
            if row["blocked"]:
                blocked.add(code)
        return cls(
            cash, available, reserved, unsettled, receivable, market, quantities, sellable, blocked
        )

    def reconcile_positions(self, broker_positions):
        # Check owned quantities only. A broker holding at an empty strategy slot is external.
        owned = {c for c, q in self.quantities.items() if q}
        matched = set()
        for row in broker_positions:
            code = row["stock_code"]
            if code not in owned:
                continue
            if code in matched:
                raise Stop(f"{code}: duplicate broker position")
            matched.add(code)
            if code in self.blocked:
                continue
            if shares(row["volume"], code) != self.quantities[code]:
                raise Stop(f"{code}: share reconciliation required")
            self.sellable[code] = min(self.sellable[code], shares(row["can_use_volume"], code))
        if owned - matched - self.blocked:
            raise Stop("Owned holding absent from broker positions")

    def buy_budget(self, cash_weight, marked_nav):
        return max(D(0), self.available - cash_weight * marked_nav)
