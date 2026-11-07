"""Offline contract tests. Every HTTP call, clock and wait is injected."""

import io
import json
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from urllib.error import HTTPError, URLError

from v12.config import BASE, CODES, digest
from v12.events import CN
from v12.execute import execute
from v12.gate.account import Account, buy_reserve
from v12.gate.client import GateClient
from v12.gate.order_loop import Stop
from v12.publish import publish

A = CODES["csi300"]
U = CODES["nasdaq"]
SP = CODES["sp500"]
DAY = "2026-11-05"
REF = "2026-11-04"
NAV = D(500000)
CASH = D(".2")


class Clock:
    def __init__(self, hour=9, minute=30, second=5):
        self.time = datetime(2026, 11, 5, hour, minute, second, tzinfo=CN)

    def now(self):
        return self.time

    def sleep(self, seconds):
        self.time += timedelta(seconds=seconds)


class Response(io.BytesIO):
    def __init__(self, data, status=200):
        super().__init__(json.dumps(data).encode())
        self.status = status


class Sim:
    def __init__(self, clock, adjustments):
        self.clock = clock
        self.notices = dict(
            status="COMPLETE",
            through=REF,
            baseline=REF,
            codes=list(CODES.values()),
            pending={},
            decisions={},
            observed_at=REF + "T17:01:00+08:00",
        )
        self.goals = {c: NAV * (1 - CASH) * D(str(b)) for c, b in zip(CODES.values(), BASE)}
        self.q = {c: int(g / 5) + adjustments.get(c, 0) for c, g in self.goals.items()}
        self.cash = NAV - 5 * sum(self.q.values())
        self.calls = []
        self.submitted = []
        self.cancels = []
        self.intents = {}
        self.refusals = {}
        self.history = []
        self.behavior = {}
        self.hold = set()
        self.partial = {}
        self.halted = set()
        self.asks = {}
        self.mismatch = set()
        self.refuse_cancel = False
        self.reported_reserve = 0
        self.stale = set()
        self.bad_health = False
        self.hide_history = False

    def plan(self):
        sides = {}
        for c, qty in self.q.items():
            diff = self.goals[c] - qty * 5
            sides[c] = ("BUY" if diff > 0 else "SELL") if abs(diff) > qty * 5 * D(".05") else "HOLD"
        obj = dict(
            version=1,
            strategy="v12",
            mapping=CODES,
            decision_date=REF,
            execute_date=DAY,
            scale=0.8,
            cash_weight=0.2,
            predicted_vol=0.15,
            reference_nav=str(NAV),
            weights={k: 0.8 * b for k, b in zip(CODES, BASE)},
            prices=dict.fromkeys(self.q, 5),
            sides=sides,
            amounts={c: str(g) for c, g in self.goals.items()},
            reference_shares=self.q.copy(),
            next_events=[],
            data_asof=REF + "T17:05:00+08:00",
            view_asof=REF + "T17:04:00+08:00",
            notices_digest=digest(self.notices),
            notice_terms=digest([REF, REF, []]),
        )
        return {**obj, "plan_id": digest(obj)}

    def view(self):
        reserve = sum(
            (
                buy_reserve(x["quantity"] - x["filled"], D(x["price"]))
                for x in self.intents.values()
                if x["state"] != "DONE" and x["side"] == "BUY"
            ),
            D(self.reported_reserve),
        )
        market = sum(self.q.values()) * 5
        return dict(
            strategy_id="v12",
            asset_type="ETF",
            accounting_cash=str(self.cash),
            available_cash=str(max(0, self.cash - reserve)),
            reserved_cash=str(reserve),
            reserved_sale_fees="0",
            reserved_buy_fees="0",
            unsettled_sale_proceeds="0",
            dividend_receivable="0",
            market_value=str(market),
            economic_equity=str(self.cash + market),
            blocked=False,
            broker_account_verified=True,
            trading_enabled=True,
            broker_observation_at=self.clock.now().timestamp(),
            positions=[
                dict(security=c, quantity=q, sellable=q, blocked=False) for c, q in self.q.items() if q
            ],
        )

    def client(self, write=True):
        return GateClient(
            "http://127.0.0.1:8001",
            "v12",
            "offline-fixture-identity-only",
            opener=self.open,
            write=write,
        )

    def refuse(self, req, reason):
        raise HTTPError(
            req.full_url, 409, "conflict", {}, io.BytesIO(json.dumps({"detail": reason}).encode())
        )

    def fill(self, i, n):
        if n <= 0:
            return
        c = i["security"]
        side = i["side"]
        existing = i["filled"]
        self.q[c] += n if side == "BUY" else -n
        amount = D(n) * 5
        fee = D(0) if existing else D(5)
        self.cash += (amount if side == "SELL" else -amount) - fee
        i["filled"] += n
        self.history.append(
            dict(
                intent_id=i["intent_id"],
                order_id=i["intent_id"],
                traded_id=str(len(self.history)),
                traded_volume=n,
                traded_amount=str(amount),
                commission=float(fee),
            )
        )
        if i["filled"] == i["quantity"]:
            i.update(state="DONE", fees_confirmed=True, final_fee_cents=500)

    def open(self, req, timeout):
        from urllib.parse import urlsplit, parse_qs

        url = urlsplit(req.full_url)
        assert url.netloc == "127.0.0.1:8001"
        path = url.path
        bits = path.split("/")
        body = json.loads(req.data) if req.data else None
        self.calls.append((req.method, path))
        if path == "/health":
            return Response(
                dict(
                    role="full",
                    connected=True,
                    bridge_version_match=True,
                    write_ready=not self.bad_health,
                )
            )
        if path.endswith("/tick") or path.endswith("/instrument"):
            code = parse_qs(url.query)["stock_code"][0]
            if path.endswith("/instrument"):
                return Response(
                    dict(
                        InstrumentStatus=47 if code in self.halted else 0,
                        VolumeMultiple=1,
                        PriceTick=0.001,
                        UpStopPrice=5.5,
                        DownStopPrice=4.5,
                    )
                )
            age = 1000 if code in self.stale else 0
            return Response(
                dict(
                    time=(self.clock.now().timestamp() - age) * 1000,
                    open=5.0,
                    lastPrice=5.0,
                    openInt=13,
                    stockStatus=3,
                    bidPrice=[5.0],
                    askPrice=[self.asks.get(code, 5.0)],
                    bidVol=[10],
                    askVol=[10],
                )
            )
        if path.endswith("/positions"):
            return Response(
                dict(
                    positions=[
                        dict(stock_code=c, volume=q + int(c in self.mismatch), can_use_volume=q)
                        for c, q in self.q.items()
                        if q
                    ]
                )
            )
        assert "/strategy-accounts/v12" in path
        if path.endswith("/v12"):
            return Response(self.view())
        if path.endswith("/intents"):
            return Response(
                dict(
                    intents=[
                        {k: i[k] for k in ("intent_id", "security", "state", "side", "request_id")}
                        for i in self.intents.values()
                        if i["state"] != "DONE"
                    ]
                )
            )
        if path.endswith("/trades"):
            return Response({"trades": self.history})
        if path.endswith("/events"):
            orders = [] if self.hide_history else list(self.intents.values())
            return Response(
                dict(event_day=DAY.replace("-", ""), orders=orders, trades=[], local_rejections=[])
            )
        if path.endswith("/orders") and body:
            rid = body["request_id"]
            c = body["stock_code"]
            self.submitted.append((self.clock.now(), deepcopy(body)))
            if rid in self.refusals:
                return Response(dict(status="REJECTED", reason=self.refusals[rid]), 202)
            if rid in self.intents:
                return Response(dict(intent_id=rid, status="EXISTING"), 202)
            script = self.behavior.get(c, [])
            action = script.pop(0) if script else None
            if action and action != "lost":
                self.refusals[rid] = action
                self.refuse(req, action)
            intent = dict(
                intent_id=rid,
                request_id=rid,
                strategy_id="v12",
                security=c,
                quantity=body["volume"],
                filled=0,
                side="BUY" if body["direction"] == 23 else "SELL",
                state="WORKING",
                price=body["limit_price"],
                fees_confirmed=False,
            )
            self.intents[rid] = intent
            if c not in self.hold:
                self.fill(intent, intent["quantity"])
            elif c in self.partial:
                self.fill(intent, min(intent["quantity"], self.partial.pop(c)))
            if action == "lost":
                raise URLError("simulated lost reply")
            return Response(dict(intent_id=rid, status=intent["state"]), 202)
        key = bits[-2] if bits[-1] in ("refresh", "cancel") else bits[-1]
        i = self.intents[key]
        if bits[-1] == "cancel":
            self.cancels.append((self.clock.now(), i["security"]))
            if self.refuse_cancel:
                self.refuse(req, "unconfirmed cancellation")
            i.update(state="DONE", fees_confirmed=True, final_fee_cents=500 if i["filled"] else 0)
        return Response(i)


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.plan = self.root / "plan.json"
        self.clock = Clock(14, 26)

    def go(self, sim, mode="send"):
        if not self.plan.exists():
            publish(self.plan, sim.plan())
        return execute(
            self.plan,
            self.root / "run",
            lambda: sim.client(mode == "send"),
            mode=mode,
            now=self.clock.now,
            sleep=self.clock.sleep,
            notices=sim.notices,
        )

    def test_suspension_then_reopen(self):
        sim = Sim(self.clock, {U: -1000})
        sim.behavior[U] = ["broker instrument is suspended from trading"]
        r = self.go(sim)
        order = [(t, b) for t, b in sim.submitted if b["stock_code"] == U]
        self.assertEqual(len(order), 2)
        self.assertGreaterEqual((order[1][0] - order[0][0]).total_seconds(), 60)
        self.assertNotEqual(order[0][1]["request_id"], order[1][1]["request_id"])
        self.assertGreater(r["rows"][-1]["filled"], 0)
        self.assertIn("ask_vs_reference", r["rows"][-1]["quote_facts"])

    def test_deadline_cancel_and_independent_buy(self):
        sim = Sim(self.clock, {U: 2000, A: -3500})
        sim.hold.add(U)
        r = self.go(sim)
        orders = {b["stock_code"]: b for _, b in sim.submitted}
        self.assertIn(U, orders)
        self.assertIn(A, orders)
        self.assertEqual(sim.cancels, [(datetime(2026, 11, 5, 14, 30, tzinfo=CN), U)])
        self.assertLess(orders[A]["volume"], 3500)
        self.assertGreaterEqual(sim.cash, 100000)
        self.assertEqual(r["legs"][U + "S"]["status"], "INCOMPLETE")

    def test_qdii_cannot_release_nonexistent_cash(self):
        sim = Sim(self.clock, {U: 2000, A: -1000})
        sim.halted.add(U)
        r = self.go(sim)
        self.assertFalse(sim.submitted)
        self.assertEqual(r["legs"][A + "B"]["reason"], "CASH_FLOOR_OR_MINIMUM_LOT")
        self.assertGreater(D(r["cash_floor_shortfall"]), 0)

    def test_busy_then_uncertain_submit(self):
        sim = Sim(self.clock, {A: -1000})
        sim.behavior[A] = [
            "gateway busy with other orders; not sent, retry with a new request_id",
            "lost",
        ]
        self.go(sim)
        reqs = [b for _, b in sim.submitted]
        self.assertEqual(len(reqs), 3)
        self.assertNotEqual(reqs[0]["request_id"], reqs[1]["request_id"])
        self.assertEqual(reqs[1], reqs[2])
        self.assertEqual(len(sim.intents), 1)

    def test_ownership_skips_only_one_leg(self):
        sim = Sim(self.clock, {U: -1000, A: -1000})
        sim.behavior[A] = ["instrument is unowned or belongs to another strategy"]
        r = self.go(sim)
        self.assertEqual(r["legs"][A + "B"]["reason"], "NOT_OWNED:" + A)
        self.assertTrue(any(t["traded_volume"] > 0 for t in sim.history))
        self.assertLessEqual(sum(t["traded_volume"] for t in sim.history), 1000)

    def test_uncertain_cancel_not_resubmitted(self):
        sim = Sim(self.clock, {U: 1000, A: -2000})
        sim.hold.add(U)
        sim.refuse_cancel = True
        r = self.go(sim)
        self.assertEqual(r["legs"][U + "S"]["status"], "UNRESOLVED")
        self.assertEqual(len(sim.cancels), 1)
        self.assertEqual(len([b for _, b in sim.submitted if b["stock_code"] == U]), 1)

    def test_no_plan_bad_plan_dry_run(self):
        never = lambda: self.fail("gateway created")
        kw = dict(mode="send", now=self.clock.now)
        self.assertEqual(execute(self.plan, self.root, never, **kw)["status"], "NOTHING_TO_DO")
        sim = Sim(self.clock, {A: -1000})
        plan = sim.plan()
        publish(self.plan, plan)
        self.assertEqual(
            execute(
                self.plan,
                self.root,
                never,
                mode="send",
                now=lambda: self.clock.now() + timedelta(days=1),
            )["status"],
            "NOTHING_TO_DO",
        )
        r = self.go(sim, "dry-run")
        self.assertEqual(r["status"], "DRY_RUN")
        self.assertTrue(all(m == "GET" for m, _ in sim.calls))
        self.plan.write_bytes(self.plan.read_bytes() + b" ")
        with self.assertRaises(ValueError):
            execute(self.plan, self.root, never, **kw)

    def test_rerun_state_bound_to_plan(self):
        sim = Sim(self.clock, {A: -1000})
        self.go(sim)
        n = len(sim.submitted)
        self.assertEqual(self.go(sim)["status"], "RECOVERED_REVIEW_REQUIRED")
        self.assertEqual(len(sim.submitted), n)
        plan = sim.plan()
        other = self.root / "other.json"
        publish(other, plan)
        with self.assertRaises(Stop):
            execute(other, self.root / "run", lambda: sim.client(), mode="send", now=self.clock.now)

    def test_reservation_and_reconciliation(self):
        self.assertEqual(buy_reserve(100, D("1.005")), D("106.51"))
        sim = Sim(self.clock, {A: -1000})
        sim.reported_reserve = 100
        with self.assertRaises(Stop):
            self.go(sim)
        self.assertFalse(sim.submitted)
        sim.reported_reserve = 0
        sim.mismatch.add(A)
        with self.assertRaises(Stop):
            self.go(sim)
        acct = Account.parse(Sim(self.clock, {}).view())
        self.assertEqual(acct.nav, NAV)

    def test_partial_and_recovery_cancel(self):
        sim = Sim(self.clock, {U: 1000, A: -2000})
        sim.hold.add(U)
        sim.partial[U] = 100
        r = self.go(sim)
        self.assertEqual(r["legs"][U + "S"]["filled"], 100)
        row = next(row for row in r["rows"] if row["code"] == U)
        self.assertEqual(D(row["fee_rule"]), 5)
        self.assertEqual(D(row["settled_fee"]), 5)

    def test_no_identity_on_restart_no_post(self):
        sim = Sim(self.clock, {A: -1000})
        self.go(sim)
        n = len(sim.submitted)
        state = self.root / "run" / DAY / "execution_state.json"
        data = json.loads(state.read_text())
        entry = next(iter(data["requests"].values()))
        entry.update(intent_id=None, last=None)
        state.write_text(json.dumps(data))
        sim.hide_history = True
        with self.assertRaises(Stop):
            self.go(sim)
        self.assertEqual(len(sim.submitted), n)
        sim.hide_history = False
        self.assertEqual(self.go(sim)["status"], "RECOVERED_REVIEW_REQUIRED")
        self.assertEqual(len(sim.submitted), n)

    def test_halt_after_submission_leaves_cancel_unverified(self):
        sim = Sim(self.clock, {U: 1000, A: -2000})
        sim.hold.add(U)
        original = sim.open

        def updated(request, timeout):
            if self.clock.now().minute >= 29:
                sim.halted.add(U)
            return original(request, timeout)

        sim.open = updated
        result = self.go(sim)
        self.assertEqual(result["status"], "STOPPED")
        self.assertEqual(result["legs"][U + "S"]["status"], "UNRESOLVED")
        self.assertEqual(sim.cancels, [])
        self.assertEqual(len([b for _, b in sim.submitted if b["stock_code"] == U]), 1)

    def test_wait_for_domestic_sell_but_not_qdii_sell(self):
        gold = CODES["gold"]
        sim = Sim(self.clock, {gold: 1000, U: 2000, A: -4500})
        sim.hold.update([gold, U])
        original = sim.open

        def finish_domestic(request, timeout):
            if self.clock.now().minute >= 27:
                for i in sim.intents.values():
                    if i["security"] == gold and i["state"] == "WORKING":
                        sim.fill(i, i["quantity"] - i["filled"])
            return original(request, timeout)

        sim.open = finish_domestic
        result = self.go(sim)
        t = next(t for t, b in sim.submitted if b["stock_code"] == A)
        self.assertEqual(t.minute, 27)
        self.assertLess(t, sim.cancels[0][0])
        self.assertEqual(result["legs"][gold + "S"]["status"], "FILLED")
        self.assertEqual(result["legs"][U + "S"]["filled"], 0)

    def test_stale_quote_and_unknown_refusal(self):
        sim = Sim(self.clock, {U: -1000})
        sim.stale.add(U)
        result = self.go(sim)
        self.assertEqual(sim.submitted, [])
        self.assertEqual(result["legs"][U + "B"]["reason"], "STALE_QUOTE")

    def test_unrecognized_rejection_stops(self):
        sim = Sim(self.clock, {A: -1000})
        sim.behavior[A] = ["unknown response to simulated order"]
        with self.assertRaises(Stop):
            self.go(sim)
        self.assertEqual(len(sim.submitted), 1)

    def test_notice_change_after_publishing_stops_before_contact(self):
        sim = Sim(self.clock, {A: -1000})
        publish(self.plan, sim.plan())
        sim.notices["pending"] = {"changed": "待核实"}
        with self.assertRaises(ValueError):
            self.go(sim)
        self.assertEqual(sim.calls, [])

    def test_qdii_ask_anchor_report_only(self):
        sim = Sim(self.clock, {U: -2000, A: -1000})
        sim.asks[U] = 5.25
        self.go(sim)
        req = next(b for _, b in sim.submitted if b["stock_code"] == U)
        self.assertEqual(D(req["limit_price"]), D("5.265"))
        report = json.loads(
            (self.root / "run" / DAY / "send" / "report.json").read_text(encoding="utf8")
        )
        row = next(r for r in report["rows"] if r["code"] == U)
        self.assertEqual(D(row["quote_facts"]["ask_vs_reference"]), D(".05"))

    def test_reprice_only_once_and_retry_continuous_evidence(self):
        sim = Sim(self.clock, {A: -1000, U: -1000})
        sim.behavior[A] = ["fixed execution price outside verified exchange band"] * 2
        sim.behavior[U] = ["broker instrument lacks the supported continuous cash-stock evidence"]
        self.go(sim)
        self.assertEqual(len([b for _, b in sim.submitted if b["stock_code"] == A]), 2)
        self.assertEqual(len([b for _, b in sim.submitted if b["stock_code"] == U]), 2)

    def test_post_close_cash_change_requires_reconciliation(self):
        sim = Sim(self.clock, {A: -1000})
        publish(self.plan, sim.plan())
        sim.cash += 1000
        with self.assertRaisesRegex(Stop, "Overnight cash"):
            self.go(sim)
        self.assertFalse(sim.submitted)

    def test_cash_mismatch_after_settlement_does_not_report_complete(self):
        sim = Sim(self.clock, {A: -1000})
        before = sim.view

        def wrong_final_view():
            view = before()
            if sim.history:
                for field in ("available_cash", "accounting_cash", "economic_equity"):
                    view[field] = str(D(view[field]) + 1)
            return view

        sim.view = wrong_final_view
        with self.assertRaisesRegex(Stop, "End strategy positions/cash"):
            self.go(sim)
        self.assertEqual(len(sim.submitted), 1)

    def test_restart_with_working_order_drains_without_new_attempt(self):
        sim = Sim(self.clock, {U: -1000})
        sim.hold.add(U)
        opener = sim.open

        def crash_after_ack(request, timeout):
            if request.full_url.endswith("/refresh"):
                raise RuntimeError("simulated executor interruption")
            return opener(request, timeout)

        sim.open = crash_after_ack
        with self.assertRaises(RuntimeError):
            self.go(sim)
        count = len(sim.submitted)
        sim.open = opener
        self.assertEqual(self.go(sim)["status"], "RECOVERED_REVIEW_REQUIRED")
        self.assertEqual(len(sim.submitted), count)
        self.assertEqual(len(sim.cancels), 1)
        self.assertTrue(all(i["state"] == "DONE" for i in sim.intents.values()))

    def test_confirmed_fees_required_before_domestic_sell_releases_buy(self):
        gold = CODES["gold"]
        sim = Sim(self.clock, {gold: 1000, A: -2000})
        opener = sim.open

        def delayed_fee(request, timeout):
            if request.full_url.endswith("/refresh"):
                for intent in sim.intents.values():
                    if intent["security"] == gold:
                        intent["fees_confirmed"] = self.clock.now().minute >= 27
            return opener(request, timeout)

        sim.open = delayed_fee
        self.go(sim)
        buy_time = next(t for t, body in sim.submitted if body["stock_code"] == A)
        self.assertGreaterEqual(buy_time.minute, 27)

    def test_recovery_settles_confirmable_legs_even_if_one_qdii_cannot_cancel(self):
        sim = Sim(self.clock, {U: -2000, SP: -2000})
        sim.hold.update((U, SP))
        opener = sim.open

        def interrupted(req, timeout):
            if req.full_url.endswith("/refresh"):
                raise RuntimeError("fixture crash")
            return opener(req, timeout)

        sim.open = interrupted
        with self.assertRaises(RuntimeError):
            self.go(sim)
        self.assertEqual(len(sim.submitted), 2)
        sim.open = opener
        sim.halted.add(U)
        with self.assertRaisesRegex(Stop, "Unresolved on recovery"):
            self.go(sim)
        self.assertEqual([code for _, code in sim.cancels], [SP])
        self.assertEqual(len(sim.submitted), 2)

    def test_mail_daily_and_error_separately_with_fake_transport(self):
        from v12.report import notify

        delivered = []
        record = dict(execute_date=DAY, status="STOPPED", mode="send")
        for _ in range(2):
            notify(
                self.root, record, {"enabled": True}, deliver=lambda title, body: delivered.append(title)
            )
        self.assertEqual(len(delivered), 2)
