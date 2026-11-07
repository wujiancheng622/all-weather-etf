"""Nightly corporate-action-aware market inputs and immutable V12 execution plans."""

import argparse
from datetime import date, datetime, time
from hashlib import sha256
import getpass
import http.client
import json
import os
from pathlib import Path
import tempfile
from urllib.parse import urlencode

import pandas as pd
from .announcements import read, reviewed_actions, save, terms_identity
from .config import BASE, CODES, KEYS, canonical_json, digest
from .events import CN, stamp
from .gate.account import Account, D, money
from .gate.client import GateClient
from .risk import target
from .sessions import checked_calendar


def history_bars(code, start, end):
    query = urlencode(
        dict(
            stock_code=code,
            period="1d",
            start_time=start.replace("-", ""),
            end_time=end.replace("-", ""),
            count=-1,
            dividend_type="none",
            fill_data="false",
            subscribe="false",
        )
    )
    conn = http.client.HTTPConnection("127.0.0.1", 8002, timeout=30)
    try:
        conn.request("GET", "/api/v1/market_data?" + query)
        reply = conn.getresponse()
        if reply.status != 200:
            raise ValueError(f"{code}: incomplete history HTTP {reply.status}")
        data = json.loads(reply.read())
        if (
            data.get("stock_code") != code
            or data.get("dividend_type") != "none"
            or data.get("fill_data") is not False
        ):
            raise ValueError(f"{code}: unexpected daily bar identity or adjustment mode")
        return data["bars"]
    finally:
        conn.close()


def merge_terms(settings, reviewed, baseline):
    events = {}
    approved = {(e["key"], e.get("ex_date") or e["effective_date"]) for e in reviewed}
    for raw in [*settings.get("dividends", []), *settings.get("splits", []), *reviewed]:
        e = dict(raw)
        if e["key"] not in KEYS:
            raise ValueError("Unknown action slot")
        if "ex_date" in e:
            dates = [date.fromisoformat(e[k]) for k in ("record_date", "ex_date", "payment_date")]
            if not dates[0] < dates[1] <= dates[2] or money(e["cash_per_actual_share"], "dividend") <= 0:
                raise ValueError("Invalid cash entitlement")
            signature = (tuple(dates), str(money(e["cash_per_actual_share"], "dividend").normalize()))
            e["cash_per_actual_share"] = float(e["cash_per_actual_share"])
        else:
            date.fromisoformat(e["effective_date"])
            f = money(e["new_shares_per_old_share"], "split")
            if f <= 0 or (f != int(f) and e.get("rounding") != "ceil"):
                raise ValueError("Unverified split rounding")
            signature = (str(f.normalize()), e.get("rounding") if f != int(f) else None)
            e["new_shares_per_old_share"] = float(f)
        key = (e["key"], e.get("ex_date") or e["effective_date"])
        if key[1] > baseline and key not in approved:
            raise ValueError("Post-baseline action missing from verified official terms")
        if key in events:
            if events[key][0] != signature:
                raise ValueError(f"Reconcile conflicting terms: {key}")
            continue
        events[key] = signature, e
    return [events[key][1] for key in sorted(events)]


def window(calendar, day, n):
    dates = checked_calendar(calendar)
    k = dates.index(day)
    if k < n:
        raise ValueError(f"Need {n} completed sessions")
    return dates[k - n : k]


def prepare_market(settings, notices, day, out, *, fetch=history_bars, now=None):
    now = now or datetime.now(CN)
    days = window(settings["calendar"], day, 64)
    ref = days[-1]
    terms = merge_terms(settings, reviewed_actions(notices, ref, now), notices["baseline"])
    by_key = {(e["key"], e.get("ex_date") or e["effective_date"]): e for e in terms}
    returns = {}
    refs = {}
    halt_marks = []
    for k, code in CODES.items():
        rows = fetch(code, days[0], ref)
        bars = {}
        for row in rows:
            trade_day = str(datetime.fromtimestamp(row["time"] / 1000, CN).date())
            if trade_day in bars or trade_day not in days:
                raise ValueError(f"{code}: invalid date {trade_day}")
            bars[trade_day] = row
        previous = None
        changes = []
        for d in days:
            event = by_key.get((k, d), {})
            factor = money(event.get("new_shares_per_old_share", 1), "factor")
            cash = money(event.get("cash_per_actual_share", 0), "cash")
            row = bars.get(d)
            halt = settings.get("confirmed_halts", {}).get(d, {}).get(code)
            if row is None or money(row["volume"], "daily volume") == 0:
                if not halt or not halt.get("source") or stamp(halt["known_at"]) > now:
                    raise ValueError(f"{code} {d}: unverified gap")
                if "verified_close" in halt:
                    close = money(halt["verified_close"], "verified halt valuation")
                elif factor != 1 and previous is not None:
                    close = previous / factor
                else:
                    raise ValueError(f"{code} {d}: verified valuation required for suspended session")
                halt_marks.append(dict(code=code, date=d, close=str(close), source=halt["source"]))
            else:
                if halt:
                    raise ValueError(f"{code} {d}: conflicting halt evidence")
                close = money(row["close"], "close")
                low, high = money(row["low"], "low"), money(row["high"], "high")
                if (
                    not 0
                    < low
                    <= min(close, money(row["open"], "open"))
                    <= max(close, money(row["open"], "open"))
                    <= high
                ):
                    raise ValueError(f"{code} {d}: inconsistent daily bar")
            if close <= 0:
                raise ValueError(f"{code}: invalid share basis")
            if previous is not None:
                changes.append(float((close * factor + cash) / previous - 1))
            previous = close
        returns[k] = changes
        refs[code] = float(previous)
    panel = pd.DataFrame(returns, index=days[1:])
    target(panel)
    updated = {
        **settings,
        "dividends": [e for e in terms if "ex_date" in e],
        "splits": [e for e in terms if "effective_date" in e],
    }
    # Publish the input directory as a unit; failed downloads never replace a good day.
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    import shutil

    temp = Path(tempfile.mkdtemp(dir=out.parent))
    try:
        panel.to_csv(temp / "returns.csv")
        session = dict(
            date=day,
            reference_date=ref,
            as_of=now.isoformat(),
            prices=refs,
            contracts={},
            returns_csv="returns.csv",
            notices_digest=digest(notices),
            terms_digest=digest(terms),
            halt_marks=halt_marks,
        )
        session["returns_sha256"] = sha256((temp / "returns.csv").read_bytes()).hexdigest()
        save(temp / "settings.json", updated)
        save(temp / "session.json", session)
        if out.exists():
            raise FileExistsError(out)
        temp.rename(out)
    finally:
        if temp.exists():
            shutil.rmtree(temp)
    return session


def build_plan(inputs, notices, view, now):
    root = Path(inputs)
    session = read(root / "session.json")
    settings = read(root / "settings.json")
    day = session["date"]
    dates = window(settings["calendar"], day, 63)
    ref = dates[-1]
    if (
        date.fromisoformat(day).weekday() >= 5
        or ref != session["reference_date"]
        or now < datetime.combine(date.fromisoformat(ref), time(17), CN)
        or now >= datetime.combine(date.fromisoformat(day), time(0), CN)
    ):
        raise ValueError("Publish after 17:00 on the decision session for its next trading day")
    reviewed = reviewed_actions(notices, ref, now)
    if session.get("notices_digest") != digest(notices):
        raise ValueError("Prepare data against the reviewed notice snapshot")
    terms = merge_terms(settings, reviewed, notices["baseline"])
    if session["terms_digest"] != digest(terms):
        raise ValueError("Corporate terms have changed")
    if sha256((root / "returns.csv").read_bytes()).hexdigest() != session.get("returns_sha256"):
        raise ValueError("Changed or incomplete signal inputs")
    cutoff = stamp(session["as_of"])
    observed = datetime.fromtimestamp(view["broker_observation_at"], CN)
    if (
        not datetime.combine(date.fromisoformat(ref), time(15), CN) <= cutoff <= now
        or str(observed.date()) != ref
        or observed.time() < time(15)
        or observed > now
    ):
        raise ValueError("Expected a completed closing observation")
    panel = pd.read_csv(root / "returns.csv", index_col=0)
    if list(panel.index) != dates:
        raise ValueError("Incomplete or future signal inputs")
    signal = target(panel)
    acct = Account.parse(view)
    if acct.reserved or acct.unsettled or acct.blocked:
        raise ValueError("Closing account not reconciled")
    prices = session["prices"]
    if set(prices) != set(CODES.values()) or any(money(p, "reference") <= 0 for p in prices.values()):
        raise ValueError("Ten positive reference closes required")
    value = sum(money(prices[c], c) * q for c, q in acct.quantities.items())
    if abs(value - acct.market_value) > D(".01") * max(1, sum(q > 0 for q in acct.quantities.values())):
        raise ValueError("Closing prices and strategy closing NAV disagree")
    if acct.nav <= 0:
        raise ValueError("A positive strategy closing NAV is required")
    amounts = {}
    sides = {}
    scale = D(str(signal["scale"]))
    for k, b in zip(KEYS, BASE):
        c = CODES[k]
        desired = D(str(b)) * scale * acct.nav
        old = acct.quantities[c] * money(prices[c], c)
        delta = desired - old
        amounts[c] = str(desired)
        sides[c] = ("BUY" if delta > 0 else "SELL") if abs(delta) > old * D(".05") else "HOLD"
    next_events = [e for e in terms if (e.get("ex_date") or e.get("effective_date")) == day]
    payload = dict(
        version=1,
        strategy="v12",
        mapping=CODES,
        decision_date=ref,
        execute_date=day,
        data_asof=cutoff.isoformat(),
        view_asof=observed.isoformat(),
        notices_digest=digest(notices),
        notice_terms=terms_identity(notices, ref, now),
        reference_nav=str(acct.nav),
        reference_shares=acct.quantities,
        prices=prices,
        amounts=amounts,
        sides=sides,
        next_events=next_events,
        **signal,
    )
    return {**payload, "plan_id": digest(payload)}


def immutable_bytes(path, blob):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as f:
        f.write(blob)
        f.flush()
        os.fsync(f.fileno())
        name = f.name
    try:
        try:
            os.link(name, path)
        except FileExistsError:
            if path.read_bytes() != blob:
                raise ValueError("An immutable plan already exists")
    finally:
        os.unlink(name)


def publish(path, plan):
    path = Path(path)
    blob = (canonical_json(plan) + "\n").encode()
    immutable_bytes(path, blob)
    immutable_bytes(path.with_suffix(path.suffix + ".sha256"), sha256(blob).hexdigest().encode())


def load_plan(path, today):
    path = Path(path)
    receipt = path.with_suffix(path.suffix + ".sha256")
    if not path.is_file():
        if receipt.exists():
            raise ValueError("Published plan is missing")
        return None
    raw = path.read_bytes()
    if not receipt.is_file() or receipt.read_text(encoding="ascii") != sha256(raw).hexdigest():
        raise ValueError("Published bytes differ from original receipt")
    plan = json.loads(raw)
    if (
        raw != (canonical_json(plan) + "\n").encode()
        or plan.get("plan_id") != digest({k: v for k, v in plan.items() if k != "plan_id"})
        or plan.get("strategy") != "v12"
        or plan.get("mapping") != CODES
        or plan.get("version") != 1
    ):
        raise ValueError("Invalid or altered V12 plan")
    scale = money(plan["scale"], "scale")
    vol = money(plan["predicted_vol"], "vol")
    nav = money(plan["reference_nav"], "reference NAV")
    epsilon = D("1e-12")
    if (
        not 0 < scale <= 1
        or nav <= 0
        or vol <= 0
        or abs(scale - min(D(1), D(".12") / vol)) > epsilon
        or abs(money(plan["cash_weight"], "cash target") + scale - 1) > epsilon
    ):
        raise ValueError("Invalid frozen V12 scaling")
    for field in ("prices", "reference_shares", "sides", "amounts"):
        if set(plan[field]) != set(CODES.values()):
            raise ValueError("Invalid plan universe")
    if set(plan["weights"]) != set(KEYS):
        raise ValueError("Invalid risk universe")
    for k, b in zip(KEYS, BASE):
        c = CODES[k]
        expected = scale * D(str(b))
        desired = money(plan["amounts"][c], "target")
        price = money(plan["prices"][c], "price")
        qty = plan["reference_shares"][c]
        if type(qty) is not int or qty < 0 or price <= 0:
            raise ValueError("Invalid plan holdings")
        value = qty * price
        delta = desired - value
        side = ("BUY" if delta > 0 else "SELL") if abs(delta) > D(".05") * value else "HOLD"
        if (
            abs(money(plan["weights"][k], k) - expected) > epsilon
            or desired != expected * nav
            or side != plan["sides"][c]
        ):
            raise ValueError("Inconsistent frozen target or intent")
    return plan if plan["execute_date"] == today else None


def main():
    ap = argparse.ArgumentParser()
    cmd = ap.add_subparsers(dest="action", required=True)
    prep = cmd.add_parser("prepare")
    for opt in ("settings", "notices", "output", "day"):
        prep.add_argument("--" + opt, required=True)
    prep.add_argument("--bars-dir", type=Path, help="Offline JSON: CODE.json containing raw daily bars")
    pub = cmd.add_parser("publish")
    for opt in ("inputs", "notices", "output"):
        pub.add_argument("--" + opt, required=True)
    src = pub.add_mutually_exclusive_group(required=True)
    src.add_argument("--closing-view", help="Previously captured simulation closing view")
    src.add_argument("--read-simulator", action="store_true")
    a = ap.parse_args()
    notices = read(a.notices)
    if a.action == "prepare":
        fetch = (
            history_bars if not a.bars_dir else lambda c, start, end: read(a.bars_dir / (c + ".json"))
        )
        prepare_market(read(a.settings), notices, a.day, a.output, fetch=fetch)
    else:
        if a.read_simulator:
            gate = GateClient("http://127.0.0.1:8001", "v12", getpass.getpass("V12 simulation token: "))
            from .execute import check_health

            check_health(gate, False)
            if gate.intents():
                raise ValueError("Closing account has pending intents")
            view = gate.view()
        else:
            view = read(a.closing_view)
        plan = build_plan(a.inputs, notices, view, datetime.now(CN))
        publish(a.output, plan)
        print(plan["plan_id"])


if __name__ == "__main__":
    main()
