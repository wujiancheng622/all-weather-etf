"""Manual simulation subaccount executor. No plan/different date means no gateway calls."""

import argparse
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN
import getpass
from pathlib import Path
import time as clock

from . import report
from .announcements import read, terms_identity
from .config import CODES, digest
from .events import CN
from .gate.account import Account, D, QDII, buy_reserve, fee_ceiling, money
from .gate.client import GateClient
from .gate.order_loop import Leg, OrderLoop, Skip, StateFile, Stop, Wait, exclusive_run
from .gate.trial import TrialAuthorization
from .publish import load_plan

DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "config/execution.json"


class NoticeBlocked(Stop, ValueError):
    """No more order POSTs; preserve and reconcile any earlier requests."""


def check_notices(notices, plan, now):
    """Local state only: ordinary non-economic updates are intentionally allowed."""
    try:
        current = read(notices) if isinstance(notices, (Path, str)) else notices
        if not isinstance(current, dict) or terms_identity(
            current, plan["decision_date"], now()
        ) != plan["notice_terms"]:
            raise ValueError("Verified notice terms differ from published plan")
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise NoticeBlocked(
            "NOTICE_BLOCKED: local coverage/read/review/terms check failed ("
            + type(exc).__name__ + "); stop new orders and review published plan"
        ) from None


@dataclass(frozen=True)
class Settings:
    start: time
    deadline: time
    buy_limit_bps: int
    sell_limit_bps: int
    qdii_poll_seconds: int
    quote_max_age_seconds: int
    sell_wait_seconds: int
    buy_wait_seconds: int
    sell_attempts: int

    @classmethod
    def load(cls, raw):
        values = dict(raw)
        if set(values.pop("qdii_codes")) != QDII:
            raise ValueError("Invalid QDII mapping")
        for key in ("start", "deadline"):
            values[key] = time.fromisoformat(values[key])
        cfg = cls(**values)
        if not time(9, 30, 5) <= cfg.start < cfg.deadline <= time(14, 30):
            raise ValueError("Invalid execution window")
        for key in (
            "qdii_poll_seconds",
            "quote_max_age_seconds",
            "sell_wait_seconds",
            "buy_wait_seconds",
            "sell_attempts",
        ):
            if type(values[key]) is not int or values[key] <= 0:
                raise ValueError(key)
        if cfg.quote_max_age_seconds > 60 or cfg.qdii_poll_seconds < 60:
            raise ValueError("Invalid quote timing")
        if (cfg.buy_limit_bps, cfg.sell_limit_bps) != (30, 100):
            raise ValueError("Unapproved limit buffers")
        return cfg


def check_health(gate, write):
    h = gate.health()
    if (
        h.get("role") != "full"
        or h.get("connected") is not True
        or h.get("bridge_version_match") is not True
    ):
        raise Stop("Gateway role/connection/version check failed")
    if write and h.get("write_ready") is not True:
        raise Stop("Gateway write readiness failed")


def tick_price(value, step):
    raw = money(value, "price")
    snapped = (raw / step).quantize(D(1), ROUND_HALF_EVEN) * step
    if raw <= 0 or abs(snapped - raw) > step / D(1000):
        raise Stop("Invalid exchange price tick")
    return snapped


def quote(gate, code, side, settings, now):
    detail = gate.instrument(code)
    tick = gate.tick(code)
    instant = now()
    if not isinstance(detail, dict) or not isinstance(tick, dict):
        raise Wait("NO_QUOTE")
    status = detail.get("InstrumentStatus")
    if (
        type(status) is not int
        or status > 0
        or type(detail.get("VolumeMultiple")) is not int
        or detail["VolumeMultiple"] != 1
        or type(tick.get("stockStatus")) is not int
        or tick["stockStatus"] != 3
        or tick.get("openInt") != 13
        or (detail.get("IsTrading") is not None and detail["IsTrading"] is not True)
    ):
        raise Wait("MARKET_STATE_UNVERIFIED")
    if type(tick.get("time")) not in (float, int):
        raise Wait("MISSING_SOURCE_TIME")
    qt = datetime.fromtimestamp(tick["time"] / 1000, CN)
    if (
        qt.date() != instant.date()
        or not 0 <= (instant - qt).total_seconds() <= settings.quote_max_age_seconds
    ):
        raise Wait("STALE_QUOTE")
    step = money(detail.get("PriceTick"), "price tick")
    if step != D(".001"):
        raise Stop("Unexpected ETF price tick")
    upper = tick_price(detail.get("UpStopPrice"), step)
    lower = tick_price(detail.get("DownStopPrice"), step)
    if lower >= upper:
        raise Stop("Invalid exchange band")
    try:
        bid, ask = tick["bidPrice"][0], tick["askPrice"][0]
        if (
            not bid
            or not ask
            or not (0 < bid <= ask)
            or not tick["bidVol"][0] > 0
            or not tick["askVol"][0] > 0
        ):
            raise Wait("NO_EXECUTABLE_DEPTH")
    except (KeyError, TypeError, IndexError):
        raise Wait("NO_EXECUTABLE_DEPTH") from None
    bid, ask = tick_price(bid, step), tick_price(ask, step)
    if not lower <= bid <= ask <= upper:
        raise Wait("BOOK_OUTSIDE_BAND")
    if (side == "BUY" and ask >= upper) or (side == "SELL" and bid <= lower):
        raise Wait("EXECUTION_AT_PRICE_LIMIT")
    anchor = (ask if side == "BUY" else bid) if code in QDII else tick_price(tick.get("open"), step)
    bps = settings.buy_limit_bps if side == "BUY" else -settings.sell_limit_bps
    rounding = ROUND_FLOOR if side == "BUY" else ROUND_CEILING
    if code not in QDII:
        rounding = ROUND_CEILING if side == "BUY" else ROUND_FLOOR
    limit = min(
        upper, max(lower, (anchor * (1 + D(bps) / 10000) / step).quantize(D(1), rounding) * step)
    )
    return limit, dict(
        code=code,
        source_at=qt.isoformat(),
        raw_open=str(anchor),
        anchor_kind="counterparty" if code in QDII else "open",
        ask=str(ask),
        bid=str(bid),
        limit=str(limit),
        iopv=tick.get("IOPV", tick.get("iopv")),
        feed_last_close=tick.get("lastClose"),
    )


def lots(goal, price, quantity, side):
    delta = (goal / price - quantity) * (1 if side == "BUY" else -1)
    if delta <= 0:
        return 0
    return int(delta) if goal == 0 and side == "SELL" else int(delta / 100) * 100


class Planner:
    def __init__(self, gate, plan, cfg, now):
        self.gate, self.plan, self.cfg, self.now = gate, plan, cfg, now
        self.shares = plan["reference_shares"].copy()
        self.reference = {c: money(p, c) for c, p in plan["prices"].items()}
        self.goals = {c: money(v, c) for c, v in plan["amounts"].items()}
        for event in plan["next_events"]:
            code = CODES[event["key"]]
            if "effective_date" in event:
                f = money(event["new_shares_per_old_share"], "split")
                self.shares[code] = int((self.shares[code] * f).to_integral_value(ROUND_CEILING))
                self.reference[code] /= f
            else:
                self.reference[code] -= money(event["cash_per_actual_share"], "dividend")

    def account(self, view=None):
        view = self.gate.view() if view is None else view
        if self.gate.write and view.get("trading_enabled") is not True:
            raise Stop("Strategy ledger trading is disabled")
        acct = Account.parse(view)
        acct.reconcile_positions(self.gate.positions())
        return acct

    def marks(self, acct):
        values = {}
        sources = {}
        for code, qty in acct.quantities.items():
            mark = self.reference[code]
            sources[code] = "REFERENCE_CARRY"
            if qty:
                snap = self.gate.tick(code)
                ts = snap.get("time") if isinstance(snap, dict) else None
                if (
                    ts
                    and snap.get("lastPrice")
                    and 0
                    <= (self.now() - datetime.fromtimestamp(ts / 1000, CN)).total_seconds()
                    <= self.cfg.quote_max_age_seconds
                ):
                    mark = money(snap["lastPrice"], code)
                    sources[code] = "FRESH_TRADE"
            values[code] = qty * mark
        return values, sources

    def cancel_check(self, code):
        if code not in QDII:
            return True
        detail = self.gate.instrument(code)
        snap = self.gate.tick(code)
        status = detail.get("InstrumentStatus")
        stamp = snap.get("time")
        return (
            type(status) is int
            and status <= 0
            and snap.get("stockStatus") == 3
            and snap.get("openInt") == 13
            and type(stamp) in (int, float)
            and 0
            <= (self.now() - datetime.fromtimestamp(stamp / 1000, CN)).total_seconds()
            <= self.cfg.quote_max_age_seconds
        )

    def preflight(self):
        view = self.gate.view()
        acct = self.account(view)
        if acct.reserved or acct.unsettled or self.gate.intents():
            raise Stop("Outstanding intents, fee reserves or unsettled sale proceeds")
        if acct.quantities != self.shares:
            raise Stop("Reconcile prior holdings and effective corporate actions before execution")
        prior_securities = sum(
            money(self.plan["prices"][c], c) * q for c, q in self.plan["reference_shares"].items()
        )
        expected_credit = D(self.plan["reference_nav"]) - prior_securities
        for e in self.plan["next_events"]:
            if "ex_date" not in e:
                continue
            c = CODES[e["key"]]
            evidence = [
                x
                for x in view.get("corporate_actions", [])
                if x["security"] == c and x["ex_date"] == e["ex_date"].replace("-", "")
            ]
            if not evidence and not self.shares[c]:
                continue
            if len(evidence) != 1 or evidence[0].get("state") not in ("OPEN", "DONE"):
                raise Stop(f"{c}: dividend entitlement needs gateway reconciliation")
            row = evidence[0]
            if row.get("record_date") != e["record_date"].replace("-", "") or money(
                row.get("cash_per_share"), c
            ) != money(e["cash_per_actual_share"], c):
                raise Stop(f"{c}: dividend terms differ")
            expected_credit += money(row.get("cash"), "dividend entitlement")
        tolerance = D(".01") * max(1, sum(q > 0 for q in acct.quantities.values()))
        if abs(acct.cash + acct.receivable - expected_credit) > tolerance:
            raise Stop("Overnight cash, entitlements or external capital changed; reconcile plan")
        return acct

    def legs(self):
        result = []
        cutoff = datetime.combine(self.now().date(), self.cfg.deadline, CN)
        for side in ("SELL", "BUY"):
            for c in CODES.values():
                if self.plan["sides"][c] != side:
                    continue
                n = lots(self.goals[c], self.reference[c], self.shares[c], side)
                result.append(
                    Leg(
                        c + side[0],
                        c,
                        side,
                        n,
                        self.cfg.sell_wait_seconds if side == "SELL" else self.cfg.buy_wait_seconds,
                        self.cfg.sell_attempts if side == "SELL" else 1,
                        cutoff,
                        c in QDII,
                    )
                )
        return result

    def make_request(self, leg, attempt, filled=0, remaining=None):
        c, side = leg.code, leg.side
        acct = self.account()
        if c in acct.blocked:
            raise Skip("POSITION_BLOCKED")
        expected = self.shares[c] + filled * (1 if side == "BUY" else -1)
        if acct.quantities[c] != expected:
            raise Stop(f"{c}: unaccounted holding change")
        price, facts = quote(self.gate, c, side, self.cfg, self.now)
        facts["reference_close"] = str(self.reference[c])
        facts["ask_vs_reference"] = str(D(facts["ask"]) / self.reference[c] - 1)
        need = lots(self.goals[c], price, acct.quantities[c], side)
        if remaining is not None:
            need = min(remaining, need)
        if not need:
            raise Skip("TARGET_REACHED")
        facts["desired_quantity"] = need
        if side == "SELL":
            need = min(need, acct.sellable[c])
            if need != acct.quantities[c]:
                need = need // 100 * 100
            if not need:
                raise Skip("NO_SELLABLE_SHARES")
        else:
            # Current gateway valuation is the fallback for stale/suspended holdings.
            # Mark owned positions locally when a fresh trade price is available.
            values, _ = self.marks(acct)
            nav = max(acct.nav, acct.cash + sum(values.values()) + acct.receivable)
            budget = acct.buy_budget(D(str(self.plan["cash_weight"])), nav)
            # Compare all residual buy goals; outstanding buys have already reserved
            # cash. Their submitted quantities count toward their own slot only.
            working = {}
            for item in self.gate.intents():
                if item.get("side") == "BUY" and item.get("state") != "DONE":
                    detail = self.gate.order(item["intent_id"])
                    if (
                        detail.get("strategy_id") != "v12"
                        or detail["security"] != item["security"]
                        or detail["request_id"] != item["request_id"]
                    ):
                        raise Stop("Inconsistent pending buy identity")
                    working[item["security"]] = (
                        working.get(item["security"], 0) + detail["quantity"] - detail["filled"]
                    )
            requests = {}
            for symbol, direction in self.plan["sides"].items():
                if direction != "BUY":
                    continue
                ref = price if symbol == c else self.reference[symbol]
                quantity = lots(
                    self.goals[symbol], ref, acct.quantities[symbol] + working.get(symbol, 0), "BUY"
                )
                requests[symbol] = buy_reserve(quantity, ref)
            total = sum(requests.values())
            if total > budget:
                budget = budget * requests[c] / total
            facts.update(
                marked_nav=str(nav),
                available_cash=str(acct.available),
                cash_floor=str(nav * D(str(self.plan["cash_weight"]))),
                leg_budget=str(budget),
            )
            lo, hi = 0, need // 100
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if buy_reserve(100 * mid, price) <= budget:
                    lo = mid
                else:
                    hi = mid - 1
            need = lo * 100
            if not need:
                raise Skip("CASH_FLOOR_OR_MINIMUM_LOT")
        if need >= 1_000_000:
            raise Stop("Gateway quantity limit")
        facts["size_reason"] = (
            "TARGET_AT_LIMIT"
            if need == facts["desired_quantity"]
            else ("CASH_BUDGET" if side == "BUY" else "SELLABLE_CAP")
        )
        tag = self.plan["plan_id"][:20]
        rid = f"v12:{tag}:{leg.key}:{attempt}"
        req = dict(
            request_id=rid,
            stock_code=c,
            volume=need,
            direction=23 if side == "BUY" else 24,
            limit_price=str(price),
            send_price=str(price),
            fee_ceiling=str(fee_ceiling(need, price)),
            order_remark=rid,
        )
        if (
            not 0
            <= (self.now() - datetime.fromisoformat(facts["source_at"])).total_seconds()
            <= self.cfg.quote_max_age_seconds
        ):
            raise Wait("QUOTE_EXPIRED_DURING_BUDGET")
        return req, facts


def execute(
    plan_path,
    run_root,
    client_factory,
    *,
    mode,
    now=lambda: datetime.now(CN),
    sleep=clock.sleep,
    config=None,
    notices=None,
    mail=None,
    deliver=None,
    trial=None,
):
    if mode not in ("dry-run", "send", "trial-send"):
        raise ValueError("Explicit dry-run, send or trial-send required")
    sending = mode != "dry-run"
    if (mode == "trial-send" and not isinstance(trial, TrialAuthorization)) or (
        trial is not None and (not isinstance(trial, TrialAuthorization) or mode == "send")
    ):
        raise ValueError("Use explicit trial-send (or dry-run) with a valid trial authorization")
    today = now().astimezone(CN).date().isoformat()
    folder = Path(run_root) / today
    try:
        plan = load_plan(plan_path, today)
    except (ValueError, KeyError, OSError, Stop):
        failure = dict(execute_date=today, mode=mode, status="STOPPED", message="PLAN_VALIDATION_FAILED")
        report.write(folder / "plan-error", failure)
        if mail and sending:
            report.notify(folder, failure, mail, deliver=deliver)
        raise
    if plan is None:
        return {"status": "NOTHING_TO_DO", "execute_date": today}
    raw = config if config is not None else read(DEFAULT_CONFIG)
    cfg = Settings.load(raw)
    state_folder = trial.folder if trial is not None else folder
    path = state_folder / "execution_state.json"
    identity = dict(day=today, strategy="v12", plan_id=plan["plan_id"], config=digest(raw))
    if trial is not None:
        identity["trial"] = trial.identity
    payload = dict(
        execute_date=today, mode=mode, status="STARTED", plan_id=plan["plan_id"], legs={}, requests={}
    )
    if trial is not None:
        payload.update(scope="ONE_DOMESTIC_ETF_ONE_ORDER", authorization=trial.identity,
                       authorization_journal=str(path))
    with exclusive_run(state_folder):
        output = folder / ("recovery" if sending and path.exists() else mode)
        output.mkdir(parents=True, exist_ok=True)
        state = gate = planner = None
        start = datetime.combine(date.fromisoformat(today), cfg.start, CN)
        cutoff = datetime.combine(start.date(), cfg.deadline, CN)

        def submission_check(request=None):
            check_notices(notices, plan, now)
            if request is not None and trial is not None:
                trial.check_submit(request, state)

        try:
            existing = path.exists()
            if existing:
                state = StateFile(path, identity)
            submission_check()
            if now() < start:
                sleep((start - now()).total_seconds())
            submission_check()  # waiting for the open must not freeze notice validity
            if now() >= cutoff and (not existing or mode == "dry-run"):
                payload["status"] = "INCOMPLETE"
                payload["message"] = "DEADLINE"
                return payload
            gate = client_factory()
            check_health(gate, sending)
            planner = Planner(gate, plan, cfg, now)
            if sending and existing:
                OrderLoop(
                    gate, state, None, now=now, sleep=sleep, cancel_check=planner.cancel_check
                ).drain(cutoff)
                payload["status"] = "RECOVERED_REVIEW_REQUIRED"
            else:
                opening = planner.preflight()
                legs = planner.legs()
                if trial is not None:
                    try:
                        legs = trial.select(legs)
                    except Skip as exc:
                        legs = []
                        payload["message"] = str(exc)

                def make_request(leg, attempt, filled=0, remaining=None):
                    if trial is not None:
                        return trial.make_request(planner, leg, attempt, filled, remaining)
                    return planner.make_request(leg, attempt, filled, remaining)
                if mode == "dry-run":
                    for leg in legs:
                        try:
                            req, facts = make_request(leg, 1)
                            submission_check()  # before admitting each displayed candidate
                            payload["legs"][leg.key] = {"proposed": req, "facts": facts}
                        except (Wait, Skip) as exc:
                            payload["legs"][leg.key] = {"wait_or_skip": str(exc)}
                    submission_check()
                    payload["status"] = "DRY_RUN"
                else:
                    state = StateFile(path, identity)
                    state.data["opening"] = dict(
                        quantities=opening.quantities,
                        cash_plus_receivable=str(opening.cash + opening.receivable),
                    )
                    state.save()
                    results = OrderLoop(
                        gate,
                        state,
                        make_request,
                        now=now,
                        sleep=sleep,
                        retry_seconds=cfg.qdii_poll_seconds,
                        quote_age=cfg.quote_max_age_seconds,
                        cancel_check=planner.cancel_check,
                        submit_check=submission_check,
                        single_attempt=trial is not None,
                    ).run(legs)
                    submission_check()
                    payload["legs"] = {
                        k: dict(
                            code=r.leg.code,
                            side=r.leg.side,
                            reference_quantity=r.leg.quantity,
                            requested=r.requested,
                            filled=r.filled,
                            status=r.status,
                            reason=r.reason,
                            intents=r.intents,
                        )
                        for k, r in results.items()
                    }
                    payload["status"] = (
                        "COMPLETE"
                        if all(r.status in ("FILLED", "SATISFIED") for r in results.values())
                        else "INCOMPLETE"
                    )
                    if any(r.status == "UNRESOLVED" for r in results.values()):
                        payload["status"] = "STOPPED"
                    if trial is not None and payload["status"] != "STOPPED":
                        payload["status"] = (
                            "TRIAL_COMPLETE" if results and all(r.status == "FILLED" for r in results.values())
                            else "TRIAL_INCOMPLETE"
                        )
            end = planner.account()
            payload["strategy_cash"] = str(end.cash)
            marked, sources = planner.marks(end)
            nav = end.cash + end.receivable + sum(marked.values())
            payload["marked_equity"] = str(nav)
            payload["position_deviation"] = {
                c: dict(
                    shares=end.quantities[c],
                    actual_yuan=str(v),
                    target_yuan=str(planner.goals[c]),
                    actual_minus_target_yuan=str(v - planner.goals[c]),
                    valuation=sources[c],
                )
                for c, v in marked.items()
            }
            payload["cash_floor_shortfall"] = str(
                max(D(0), D(str(plan["cash_weight"])) * max(nav, end.nav) - end.available)
            )
            payload["deviation_shares"] = {
                c: str(planner.goals[c] / planner.reference[c] - q) for c, q in end.quantities.items()
            }
            if state is not None:
                payload["requests"] = state.data["requests"]
                report.reconcile(payload, gate.trades())
                if sending:
                    if gate.intents() or end.reserved or end.unsettled:
                        payload["reconciliation"] = {"status": "UNSETTLED"}
                        payload["status"] = "STOPPED"
                    elif "opening" in state.data:
                        payload["reconciliation"] = report.closing_check(
                            state.data["opening"], end, payload["rows"]
                        )
                    elif state.data["requests"]:
                        raise Stop("Cannot reconcile persisted requests without opening snapshot")
            submission_check()  # no stale dry-run candidates / false business completion
            return payload
        except NoticeBlocked as exc:
            payload.update(status="STOPPED", message=str(exc), candidates_valid=False)
            if mode == "dry-run":
                payload["legs"] = {}  # old candidates must not look executable
            if sending and state is not None and state.data["requests"]:
                # Never replay an uncertain submit here: it may never have arrived.
                # drain only queries identities, refreshes fills and safely cancels.
                try:
                    gate = gate or client_factory()
                    planner = planner or Planner(gate, plan, cfg, now)
                    OrderLoop(gate, state, None, now=now, sleep=sleep,
                              cancel_check=planner.cancel_check).drain(cutoff)
                except Exception as cleanup:
                    payload["cleanup_message"] = (
                        str(cleanup) if isinstance(cleanup, Stop) else type(cleanup).__name__
                    )
                payload["requests"] = state.data["requests"]
                try:
                    end = planner.account()
                    report.reconcile(payload, gate.trades())
                    payload["strategy_cash"] = str(end.cash)
                    if gate.intents() or end.reserved or end.unsettled:
                        payload["reconciliation"] = {"status": "UNSETTLED"}
                    elif "opening" in state.data:
                        payload["reconciliation"] = report.closing_check(
                            state.data["opening"], end, payload["rows"]
                        )
                except Exception as accounting:
                    payload["reconciliation"] = {"status": "UNVERIFIED", "reason": type(accounting).__name__}
            raise
        except Exception as exc:
            payload["status"] = "STOPPED"
            payload["message"] = str(exc) if isinstance(exc, (Stop, Skip, Wait)) else type(exc).__name__
            raise
        finally:
            if state is not None:
                payload["requests"] = state.data["requests"]
            report.write(output, payload)
            if mail and sending:
                report.notify(folder, payload, mail, deliver=deliver)


def main():
    ap = argparse.ArgumentParser(description="手工启动模拟盘 V12 执行器")
    ap.add_argument("--plan", required=True, type=Path)
    ap.add_argument("--run-root", required=True, type=Path)
    ap.add_argument(
        "--notices", required=True, type=Path, help="Current corporate-action discovery/review state"
    )
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--mail-settings", type=Path)
    modes = ap.add_mutually_exclusive_group(required=True)
    modes.add_argument("--dry-run", action="store_true")
    modes.add_argument("--send", action="store_true", help="Full planned execution, NOT a one-lot trial")
    modes.add_argument("--trial-send", action="store_true", help="One explicitly authorized domestic ETF order")
    ap.add_argument("--trial-id", help="Stable authorization ID; reuse for recovery, never invent a new ID to retry")
    ap.add_argument("--trial-code", choices=sorted(set(CODES.values()) - QDII))
    ap.add_argument("--trial-side", choices=("BUY", "SELL"))
    args = ap.parse_args()
    trial = None
    if args.trial_send or any((args.trial_id, args.trial_code, args.trial_side)):
        if args.send or not all((args.trial_id, args.trial_code, args.trial_side)):
            ap.error("trial requires --trial-send (or --dry-run), --trial-id, --trial-code and --trial-side")
        trial = TrialAuthorization(args.trial_id, args.trial_code, args.trial_side)

    def client():
        return GateClient(
            "http://127.0.0.1:8001", "v12", getpass.getpass("V12 simulation token: "), write=args.send or args.trial_send
        )

    result = execute(
        args.plan,
        args.run_root,
        client,
        mode="trial-send" if args.trial_send else "send" if args.send else "dry-run",
        trial=trial,
        notices=args.notices,
        config=read(args.config),
        mail=read(args.mail_settings) if args.mail_settings else None,
    )
    print(result["status"])


if __name__ == "__main__":
    main()
