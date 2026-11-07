"""Adapted from R5 live_exec/report.py (2026-11-07). Scope: V12 simulator only."""

from __future__ import annotations

import json
import smtplib
from decimal import Decimal
from email.header import Header
from email.mime.text import MIMEText
from pathlib import Path

ETF_RATE, PLAN_RATE, MIN_FEE = Decimal("0.00005"), Decimal("0.0001"), Decimal(5)


def _fee(amount, rate):
    return max(amount * rate, MIN_FEE) if amount else Decimal(0)


def reconcile(payload, trades):
    """Reconcile each persisted attempt against scoped fills; never count another strategy's trades."""
    from .gate.order_loop import Stop

    by_intent = {}
    seen = set()
    for trade in trades:
        identity = (trade.get("intent_id"), trade.get("order_id"), trade.get("traded_id"))
        if None in identity or identity in seen:
            raise Stop("Missing or duplicate trade identity")
        seen.add(identity)
        by_intent.setdefault(identity[0], []).append(trade)
    rows = []
    for rid, entry in payload["requests"].items():
        request = entry["request"]
        details = by_intent.get(entry["intent_id"], [])
        qty = sum(t["traded_volume"] for t in details)
        last = entry["last"] or {}
        if qty != last.get("filled", 0):
            raise Stop(f"{rid}: scoped trades and intent fill disagree")
        amount = sum((Decimal(str(t["traded_amount"])) for t in details), Decimal(0))
        facts = entry.get("facts", {})
        side = "BUY" if request["direction"] == 23 else "SELL"
        anchor = Decimal(facts.get("raw_open", request["limit_price"]))
        avg = amount / qty if qty else None
        deviation = (
            None if avg is None else float((avg / anchor - 1) * 10000 * (1 if side == "BUY" else -1))
        )
        final_fee = last.get("final_fee_cents")
        rows.append(
            dict(
                key=rid,
                code=request["stock_code"],
                side=side,
                requested=request["volume"],
                filled=qty,
                trade_quantity=qty,
                status="FILLED" if qty == request["volume"] else last.get("state", "REFUSED"),
                reason=(entry["refused"] or {}).get("reason", last.get("reason") or ""),
                raw_open=str(anchor),
                limit=request["limit_price"],
                avg_price=None if avg is None else str(avg),
                deviation_bps=deviation,
                fee_rule=str(_fee(amount, ETF_RATE)),
                fee_planner=str(_fee(amount, PLAN_RATE)),
                settled_fee=None if final_fee is None else str(Decimal(final_fee) / 100),
                traded_amount=str(amount),
                broker_fee=None
                if any(t.get("commission") is None for t in details)
                else str(sum((Decimal(str(t["commission"])) for t in details), Decimal(0))),
                quote_facts=facts,
            )
        )
    payload["rows"] = rows
    payload["summary"] = summarize(rows)


def closing_check(opening, account, rows):
    """Cash plus receivables is invariant to a dividend moving from receivable to paid."""
    from .gate.order_loop import Stop

    quantities = opening["quantities"].copy()
    credit = Decimal(opening["cash_plus_receivable"])
    for row in rows:
        sign = 1 if row["side"] == "BUY" else -1
        quantities[row["code"]] += sign * row["filled"]
        if row["filled"] and row["settled_fee"] is None:
            raise Stop("Filled order lacks final ledger fee")
        credit -= sign * Decimal(row["traded_amount"]) + Decimal(row["settled_fee"] or "0")
    delta = account.cash + account.receivable - credit
    if quantities != account.quantities or abs(delta) > Decimal(".01"):
        raise Stop("End strategy positions/cash disagree with confirmed scoped fills and fees")
    return dict(status="RECONCILED", cash_difference=str(delta), shares_match=True)


def summarize(rows):
    filled = [r for r in rows if r["deviation_bps"] is not None]
    weights = [Decimal(str(r["trade_quantity"])) * Decimal(r["raw_open"]) for r in filled]
    weighted = (
        (
            sum((Decimal(str(r["deviation_bps"])) * w for r, w in zip(filled, weights)), Decimal(0))
            / sum(weights, Decimal(0))
        )
        if filled
        else None
    )
    return {
        "orders": len(rows),
        "filled_orders": sum(r["status"] == "FILLED" for r in rows),
        "not_filled": [
            f"{r['code']}:{r['status']}:{r['reason']}" for r in rows if r["status"] != "FILLED"
        ],
        "mean_deviation_bps_signed": None
        if weighted is None
        else float(weighted.quantize(Decimal("0.01"))),
        "over_5bps": sum(r["deviation_bps"] > 5 for r in filled),
        "over_15bps": sum(r["deviation_bps"] > 15 for r in filled),
        "fee_rule_total": str(sum((Decimal(r["fee_rule"]) for r in rows), Decimal(0))),
        "fee_planner_total": str(sum((Decimal(r["fee_planner"]) for r in rows), Decimal(0))),
    }


def write(run_dir, payload):
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "report.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1, default=str), encoding="utf-8"
    )
    text = render(payload)
    (run_dir / "report.txt").write_text(text, encoding="utf-8")
    return text


def render(payload):
    lines = [
        f"V12 模拟执行 {payload.get('execute_date')}  模式 {payload.get('mode')}  结果 {payload['status']}"
    ]
    if payload.get("message"):
        lines.append(f"说明：{payload['message']}")
    summary = payload.get("summary")
    if summary:
        lines.append(
            f"委托 {summary['orders']} 笔，完整成交 {summary['filled_orders']} 笔；"
            f"平均偏离（带正负，按成交额加权）{summary['mean_deviation_bps_signed']} bps；"
            f"超 5bps {summary['over_5bps']} 笔、超 15bps {summary['over_15bps']} 笔"
        )
        lines.append(
            f"费用：账本规则 {summary['fee_rule_total']} 元，规划假设 {summary['fee_planner_total']} 元"
        )
        if summary["not_filled"]:
            lines.append("没成交/没完整成交：" + "；".join(summary["not_filled"]))
    for row in payload.get("rows") or ():
        lines.append(
            f"  {row['key']} {row['code']} {row['side']} 需 {row['requested']} 成交 {row['filled']} "
            f"均价 {row['avg_price']} 参考 {row['raw_open']} 限价 {row['limit']} "
            f"偏离 {row['deviation_bps']}bps {row['status']} {row['reason']}"
        )
    for key, leg in payload.get("legs", {}).items():
        lines.append(f"  {key} {leg}")
    if "deviation_shares" in payload:
        lines.append(f"按参考价折算的目标剩余份数 {payload['deviation_shares']}")
    if payload.get("strategy_cash") is not None:
        lines.append(f"策略视图现金 {payload['strategy_cash']} 元（共享账户，不与柜台总现金比较）")
    return "\n".join(lines) + "\n"


def notify(run_dir, payload, settings, *, deliver=None):
    """Optional V12 TLS mail relay; tests inject deliver. Store at most one daily summary."""
    if not settings.get("enabled"):
        return "DISABLED"
    folder = Path(run_dir)

    def relay(subject, text):
        message = MIMEText(text, "plain", "utf-8")
        sender = settings["sender"]
        receiver = settings["receiver"]
        message["From"], message["To"] = sender, receiver
        message["Subject"] = Header(subject, "utf-8")
        import ssl

        with smtplib.SMTP_SSL(
            settings["host"],
            int(settings.get("port", 465)),
            context=ssl.create_default_context(),
            timeout=15,
        ) as server:
            server.sendmail(sender, [receiver], message.as_string())

    send = deliver or relay
    kinds = ["summary"]
    if payload["status"] in ("ERROR", "STOPPED"):
        kinds.insert(0, "error")
    for kind in kinds:
        marker = folder / f"mail-{payload['execute_date']}-{kind}.json"
        try:
            with marker.open("x", encoding="utf8") as f:
                f.write('{"status":"PENDING"}')
        except FileExistsError:
            continue
        try:
            send(f"V12 {kind} {payload['execute_date']} {payload['status']}", render(payload))
        except Exception:
            marker.write_text('{"status":"UNCONFIRMED"}', encoding="utf8")
            return "UNCONFIRMED"
        marker.write_text('{"status":"SENT"}', encoding="utf8")
    return "SENT_OR_ALREADY_RECORDED"
