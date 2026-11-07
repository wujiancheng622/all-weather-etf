import numpy as np
from .config import KEYS
from .account import fee, affordable, plan_quantities

def replay_day(book, date, target, data, cfg, mode, adv):
    c = cfg.flat
    panels = data.panels
    calendar = panels["close"].index
    known = calendar[calendar.get_loc(date)-1]
    previous = book.previous_nav
    prevprice = panels["close"].loc[known].to_numpy(float)
    opening = panels["open"].loc[date].to_numpy(float)
    closing = panels["close"].loc[date].to_numpy(float)
    prevhold = np.array(book.holdings)
    prev_ar = sum(book.receivables.values())
    q = plan_quantities(book.holdings, book.cash, prev_ar, prevprice, target, c)
    if mode == "constrained_open_proxy":
        bounds = np.floor(adv.loc[date, list(KEYS)].to_numpy()*cfg.raw["execution"]["adv_fraction"]/100)*100
        if not np.isfinite(bounds).all():
            raise ValueError("Prior ADV history required")
        q = (np.sign(q)*np.minimum(np.abs(q), bounds)).astype(np.int64)
        sells = np.minimum(q, 0)
        budget = book.cash-float(sells@prevprice)-sum(fee(-sells[i]*prevprice[i], c) for i in np.flatnonzero(sells))
        q = sells+affordable(np.maximum(q, 0), prevprice, budget, c)
    raw_plan = q.copy()
    cash_before_open = book.cash
    q, logs = book.day_start(date, data.dividends, data.splits, q)
    dividend_pnl = np.zeros(len(KEYS))
    for event in logs:
        if event["kind"] == "receivable":
            dividend_pnl[KEYS.index(event["asset"])] += event["amount"]
    overnight = np.array(book.holdings)*opening-prevhold*prevprice+dividend_pnl
    tradable = panels["volume"].loc[date].to_numpy(float) > 0
    if mode == "constrained_open_proxy":
        tradable &= np.abs(panels["high"].loc[date].to_numpy()-panels["low"].loc[date].to_numpy()) >= 1e-10
    trades, cost, notional = [], 0., 0.
    def execute(i, qty):
        nonlocal cost, notional
        if not qty:
            return
        amount = abs(qty)*opening[i]
        charge = fee(amount, c)
        book.fill(KEYS[i], int(qty), float(opening[i]), charge)
        cost += charge
        notional += amount
        trades.append({"date": date, "signal_last_date": known, "asset": KEYS[i], "signed_shares": int(qty),
                       "shares": abs(int(qty)), "side": "buy" if qty > 0 else "sell", "price_proxy": float(opening[i]),
                       "notional": float(amount), "cost": charge, "cash_after": book.cash})
    for i in np.where((q < 0) & tradable)[0]:
        execute(i, max(int(q[i]), -book.holdings[i]))
    request = np.where((q > 0) & tradable, q, 0)
    budget = min(book.cash, cash_before_open) if mode == "constrained_open_proxy" else book.cash
    buys = affordable(request, opening, budget, c)
    for i in np.flatnonzero(buys):
        execute(i, int(buys[i]))
    pnl = overnight+np.array(book.holdings)*(closing-opening)
    logs += book.day_close(date, closing, data.dividends)
    nav = book.previous_nav
    error = nav-(previous+float(pnl.sum())-cost)
    if abs(error) > 1e-5:
        raise RuntimeError("NAV identity failed")
    weights = np.array(book.holdings)*closing/nav
    return {"ledger": {"date": date, "nav": nav, "daily_return": nav/previous-1,
                       "cash": book.cash, "receivable": sum(book.receivables.values()), "cost": cost,
                       "trade_count": len(trades), "two_way_turnover": notional/previous,
                       "signal_last_date": known, "market_pnl": float(pnl.sum()), "accounting_error": error},
            "trades": trades, "plans": {"date": date, **dict(zip(KEYS, map(int, raw_plan)))},
            "holdings": {"date": date, **dict(zip(KEYS, book.holdings))},
            "weights": {"date": date, **dict(zip(KEYS, map(float, weights))), "cash": book.cash/nav,
                        "receivable": sum(book.receivables.values())/nav},
            "events": [{"date": date, **event} for event in logs]}
