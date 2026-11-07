"""Shared fee, position planning and corporate-action accounting primitives."""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
import math
import numpy as np
from .config import KEYS


def fee(notional: float, cfg: dict) -> float:
    if not math.isfinite(notional) or notional < 0:
        raise ValueError("Invalid notional")
    return max(cfg["minimum_commission"], notional*cfg["commission_rate"])+notional*cfg["slippage_rate"] if notional > 1e-12 else 0.


def affordable(request, prices, cash: float, cfg: dict):
    request = np.asarray(request, dtype=np.int64)
    prices = np.asarray(prices, float)
    lot = cfg["lot_size"]
    if (request < 0).any() or (request % lot != 0).any() or not np.isfinite(prices).all() or (prices <= 0).any():
        raise ValueError("Invalid buy request")
    def quantities(a):
        return (np.floor(request*a/lot+1e-12)*lot).astype(np.int64)
    def need(q):
        return float(q@prices+sum(fee(q[i]*prices[i], cfg) for i in np.flatnonzero(q)))
    if need(request) <= cash+1e-9:
        return request.copy()
    low, high = 0., 1.
    for _ in range(55):
        mid = (low+high)/2
        if need(quantities(mid)) <= cash:
            low = mid
        else:
            high = mid
    return quantities(low)


def plan_quantities(holdings, cash, receivable, prices, target, cfg):
    holdings = np.asarray(holdings, np.int64)
    prices, target = np.asarray(prices, float), np.asarray(target, float)
    if holdings.shape != prices.shape or target.shape != prices.shape:
        raise ValueError("Position/target dimension mismatch")
    if not np.isfinite(prices).all() or (prices <= 0).any() or not np.isfinite(target).all() or (target < 0).any() or target.sum() > 1+1e-10:
        raise ValueError("Invalid prices or long-only target")
    values = holdings*prices
    nav = float(values.sum()+cash+receivable)
    delta = target*nav-values
    relative = np.divide(np.abs(delta), values, out=np.full_like(values, np.inf), where=values > 0)
    eligible = relative > cfg["relative_buffer"]
    lot = cfg["lot_size"]
    q = (np.sign(delta)*np.floor(np.abs(delta)/prices/lot+1e-12)*lot*eligible).astype(np.int64)
    q = np.maximum(q, -holdings)
    q[(target == 0) & (holdings > 0)] = -holdings[(target == 0) & (holdings > 0)]
    sells = np.minimum(q, 0)
    budget = cash-float(sells@prices)-sum(fee(-sells[i]*prices[i], cfg) for i in np.flatnonzero(sells))
    return sells+affordable(np.maximum(q, 0), prices, budget, cfg)


@dataclass
class Book:
    cash: float
    holdings: list[int]
    start_date: str
    last_date: str | None = None
    previous_nav: float = 0.
    locked: dict[str, float] = field(default_factory=dict)
    receivables: dict[str, float] = field(default_factory=dict)
    applied_actions: list[str] = field(default_factory=list)

    @classmethod
    def new(cls, cash: float, start_date: str):
        return cls(float(cash), [0]*len(KEYS), start_date, previous_nav=float(cash))

    @classmethod
    def restore(cls, raw: dict):
        if set(raw) != set(cls.__dataclass_fields__):
            raise ValueError("Incomplete or unknown account state")
        book = cls(**raw)
        book.validate()
        return book

    def validate(self):
        if len(self.holdings) != len(KEYS) or any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in self.holdings):
            raise ValueError("Invalid share state")
        values = [self.cash, self.previous_nav, *self.locked.values(), *self.receivables.values()]
        if any(not math.isfinite(v) or v < -1e-6 for v in values):
            raise ValueError("Invalid cash/entitlement state")
        if self.previous_nav <= 0:
            raise ValueError("Positive previous marked NAV required")

    def export(self):
        self.validate()
        return asdict(self)

    def nav(self, prices):
        return float(self.cash+np.asarray(self.holdings)@np.asarray(prices, float)+sum(self.receivables.values()))

    def fill(self, key: str, signed_quantity: int, price: float, cost: float):
        if key not in KEYS or isinstance(signed_quantity, bool) or not isinstance(signed_quantity, int) or signed_quantity == 0:
            raise ValueError("Invalid fill quantity/asset")
        if not math.isfinite(price) or price <= 0 or not math.isfinite(cost) or cost < 0:
            raise ValueError("Invalid fill price/fee")
        i = KEYS.index(key)
        next_holding = self.holdings[i]+signed_quantity
        next_cash = self.cash-signed_quantity*price-cost
        if next_holding < 0 or next_cash < -1e-6:
            raise ValueError("Fill violates share/cash availability")
        self.holdings[i], self.cash = next_holding, next_cash

    def day_start(self, date: str, dividends: list[dict], splits: list[dict], plans=None):
        if self.last_date is not None and date <= self.last_date:
            raise ValueError("Day already processed or out of order")
        q = None if plans is None else np.asarray(plans, np.int64).copy()
        logs = []
        for event in splits:
            action = "split:"+event["event_id"]
            if event["effective_date"] == date and action not in self.applied_actions:
                i, factor = KEYS.index(event["key"]), float(event["new_shares_per_old_share"])
                before = self.holdings[i]
                self.holdings[i] = math.ceil(before*factor-1e-10)
                if q is not None:
                    q[i] = int(np.sign(q[i])*math.floor(abs(q[i])*factor/100+1e-12)*100)
                self.applied_actions.append(action)
                logs.append({"kind": "split", "asset": event["key"], "before": before, "after": self.holdings[i]})
        for event in dividends:
            eid = event["event_id"]
            action = "ex:"+eid
            if event["ex_date"] == date and action not in self.applied_actions:
                if event["record_date"] >= self.start_date:
                    if eid not in self.locked:
                        raise ValueError("Missing record-date entitlement: incomplete account state")
                    self.receivables[eid] = self.locked[eid]
                    logs.append({"kind": "receivable", "asset": event["key"], "amount": self.locked[eid]})
                self.applied_actions.append(action)
        return q, logs

    def day_close(self, date: str, prices, dividends: list[dict]):
        logs = []
        for event in dividends:
            eid = event["event_id"]
            if event["record_date"] == date:
                amount = self.holdings[KEYS.index(event["key"])]*event["cash_per_actual_share"]
                if eid in self.locked and abs(self.locked[eid]-amount) > 1e-8:
                    raise ValueError("Duplicate conflicting dividend entitlement")
                self.locked[eid] = float(amount)
                logs.append({"kind": "entitlement", "asset": event["key"], "amount": float(amount)})
            action = "pay:"+eid
            if event["payment_date"] == date and action not in self.applied_actions:
                if event["record_date"] >= self.start_date:
                    if eid not in self.receivables:
                        raise ValueError("Missing receivable: incomplete account state")
                    amount = self.receivables.pop(eid)
                    self.cash += amount
                    logs.append({"kind": "paid_at_close", "asset": event["key"], "amount": amount})
                self.applied_actions.append(action)
        self.previous_nav, self.last_date = self.nav(prices), date
        self.validate()
        return logs
