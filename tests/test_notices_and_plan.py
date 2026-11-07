"""Public-discovery fixtures, verified terms, raw returns and nightly admission; offline only."""

import json
from datetime import datetime, time
from decimal import Decimal as D
from pathlib import Path
import tempfile
import unittest
from urllib.parse import urlsplit, parse_qs
import numpy as np
import pandas as pd
from v12 import announcements as ann
from v12.config import CODES
from v12.events import CN
from v12.execute import execute
from v12.publish import build_plan, load_plan, prepare_market, publish
from v12.risk import target
from test_gateway_sim import Sim, Clock, A, REF, DAY

NOW = datetime(2026, 11, 4, 18, tzinfo=CN)
EMPTY = "<table><tr><th>权益登记日</th></tr><tr><td>暂无分红记录</td></tr></table><table><tr><th>拆分折算日</th></tr></table>"


class PublicSource:
    def __init__(self):
        self.rows = {
            c: [dict(FUNDCODE=c[:6], ID="listing", TITLE="上市公告", PUBLISHDATE="2021-01-01")]
            for c in CODES.values()
        }
        self.html = {c: EMPTY for c in CODES.values()}

    def fetch(self, url):
        parts = urlsplit(url)
        if parts.hostname == "api.fund.eastmoney.com":
            q = parse_qs(parts.query)
            c = q["fundcode"][0]
            n = int(q["pageIndex"][0])
            rows = self.rows[next(code for code in self.rows if code.startswith(c))]
            return json.dumps(
                dict(PageIndex=n, TotalCount=len(rows), Data=rows[(n - 1) * 100 : n * 100], ErrCode=0)
            ).encode()
        c = parts.path.split("_")[-1][:6]
        return self.html[next(code for code in self.rows if code.startswith(c))].encode()

    def notice(self, i, title="收益分配公告", code=A):
        return dict(FUNDCODE=code[:6], ID=str(i), TITLE=title, PUBLISHDATE=REF)


class DiscoveryPlanTests(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.root = Path(t.name)
        self.file = self.root / "discovery.json"
        self.src = PublicSource()
        self.dates = pd.bdate_range(end=DAY, periods=65).strftime("%Y-%m-%d").tolist()
        self.settings = dict(start=DAY, calendar=self.dates)
        self.sim = Sim(Clock(), {})
        self.inputs = self.root / "day"

    def scan(self):
        return ann.scan(self.file, REF, "2026-11-03", self.src.fetch, NOW)

    def bars(self, code, first, last):
        assert first == self.dates[0] and last == REF
        result = []
        for i, d in enumerate(self.dates[:-1]):
            price = 5.0 if i % 2 else 5.04
            if code == A and d < REF:
                price += 0.1
            result.append(
                dict(
                    time=datetime.combine(pd.Timestamp(d).date(), time(15), CN).timestamp() * 1000,
                    open=price,
                    close=price,
                    low=price,
                    high=price,
                    volume=200,
                )
            )
        return result

    def close_view(self):
        view = self.sim.view()
        view["broker_observation_at"] = NOW.timestamp()
        return view

    def test_scan_review_terms_prepare_publish(self):
        self.src.rows[A] = [self.src.notice(i, "例行报告") for i in range(100)] + [self.src.notice(100)]
        state = self.scan()
        self.assertEqual(len(state["changes"]), 101)
        self.assertEqual(len(state["pending"]), 1)
        with self.assertRaises(ValueError):
            prepare_market(self.settings, state, DAY, self.inputs, fetch=self.bars, now=NOW)
        rev = next(iter(state["pending"]))
        cash = dict(
            key="csi300",
            record_date=self.dates[-3],
            ex_date=REF,
            payment_date=DAY,
            cash_per_actual_share=0.1,
        )
        ann.review(
            self.file,
            [
                dict(
                    revision=rev,
                    decision="accept",
                    official_source="https://fund.example/official",
                    note="fixture reviewed",
                    event=cash,
                )
            ],
            NOW,
        )
        state = self.scan()
        self.assertFalse(state["pending"])
        self.assertFalse(state["changes"])
        prepare_market(self.settings, state, DAY, self.inputs, fetch=self.bars, now=NOW)
        returns = pd.read_csv(self.inputs / "returns.csv", index_col=0)
        self.assertAlmostEqual(returns.iloc[-1]["csi300"], 5.1 / 5.14 - 1)
        plan = build_plan(self.inputs, state, self.close_view(), NOW)
        self.assertEqual(plan["weights"], target(returns)["weights"])
        self.assertEqual(plan["decision_date"], REF)
        file = self.root / "plan.json"
        publish(file, plan)
        publish(file, plan)
        self.assertEqual(load_plan(file, DAY), plan)
        with self.assertRaises(ValueError):
            publish(file, {**plan, "reference_nav": "500001"})
        # An amended official notice requires a new review. Earlier terms do not silently pass.
        self.src.rows[A][-1]["TITLE"] = "收益分配更正公告"
        dirty = self.scan()
        self.assertEqual(len(dirty["pending"]), 1)
        with self.assertRaises(ValueError):
            build_plan(self.inputs, dirty, self.close_view(), NOW)

    def test_interrupted_or_invalid_pagination_blocks_publication(self):
        self.scan()
        self.src.rows[A] = [self.src.notice(0), self.src.notice(0)]
        with self.assertRaises(ValueError):
            self.scan()
        self.assertEqual(ann.read(self.file)["status"], "INCOMPLETE")
        with self.assertRaises(ValueError):
            ann.reviewed_actions(ann.read(self.file), REF, NOW)

    def test_table_disagrees_or_missing(self):
        calls = []

        def f(url):
            if "fhsp_" in url:
                calls.append(url)
                if len(calls) % 2 == 0:
                    return EMPTY.replace("暂无分红记录", "2026-11-04").encode()
            return self.src.fetch(url)

        with self.assertRaises(ValueError):
            ann.scan(self.file, REF, "2026-11-03", f, NOW)
        with self.assertRaises(ValueError):
            ann.economic_tables(b"<table></table>")

    def test_table_detects_dividend_without_title_hit(self):
        self.src.html[A] = EMPTY.replace("暂无分红记录", REF)
        state = self.scan()
        self.assertEqual(next(iter(state["pending"].values()))["item"]["kind"], "dividend")

    def test_partial_market_history_no_plan_no_gateway(self):
        state = self.scan()
        with self.assertRaises(ValueError):
            prepare_market(
                self.settings, state, DAY, self.inputs, fetch=lambda *a: self.bars(*a)[:-1], now=NOW
            )
        self.assertFalse(self.inputs.exists())
        p = self.root / "plan.json"
        r = execute(
            p,
            self.root / "run",
            lambda: self.fail("unexpected gateway call"),
            mode="send",
            now=Clock().now,
        )
        self.assertEqual(r["status"], "NOTHING_TO_DO")

    def test_complete_nightly_to_fake_gateway(self):
        state = self.scan()
        prepare_market(self.settings, state, DAY, self.inputs, fetch=self.bars, now=NOW)
        plan = build_plan(self.inputs, state, self.close_view(), NOW)
        path = self.root / "plan.json"
        publish(path, plan)
        result = execute(
            path,
            self.root / "execution",
            lambda: self.sim.client(),
            mode="send",
            notices=self.file,
            now=self.sim.clock.now,
            sleep=self.sim.clock.sleep,
        )
        self.assertTrue(result["rows"])
        self.assertEqual(
            sum(r["filled"] for r in result["rows"]), sum(t["traded_volume"] for t in self.sim.history)
        )
        self.assertAlmostEqual(float(result["marked_equity"]), 500000 - 5 * len(self.sim.history))
        self.assertGreaterEqual(self.sim.cash, 0)

    def test_stale_coverage_and_split_adjusted_returns(self):
        state = self.scan()
        stale = {**state, "observed_at": REF + "T16:59:59+08:00"}
        with self.assertRaises(ValueError):
            ann.reviewed_actions(stale, REF, NOW)
        split = dict(
            key="gold",
            effective_date=self.dates[-3],
            new_shares_per_old_share=2,
            event_id="fixture-split",
        )
        cfg = {**self.settings, "splits": [split]}

        def bars(*args):
            rows = self.bars(*args)
            if args[0] == CODES["gold"]:
                for row in rows[:-2]:
                    for field in ("open", "close", "low", "high"):
                        row[field] *= 2
            return rows

        prepare_market(cfg, state, DAY, self.inputs, fetch=bars, now=NOW)
        r = pd.read_csv(self.inputs / "returns.csv", index_col=0)
        np.testing.assert_allclose(r.gold, r.sp500)

    def test_closing_equity_and_strict_buffer(self):
        state = self.scan()
        prepare_market(self.settings, state, DAY, self.inputs, fetch=self.bars, now=NOW)
        wrong = self.close_view()
        wrong["market_value"] = "1"
        wrong["economic_equity"] = str(D(wrong["accounting_cash"]) + 1)
        with self.assertRaisesRegex(ValueError, "closing NAV"):
            build_plan(self.inputs, state, wrong, NOW)
        future = self.close_view()
        future["broker_observation_at"] += 60
        with self.assertRaisesRegex(ValueError, "closing observation"):
            build_plan(self.inputs, state, future, NOW)
        # Scale=1 in this low-volatility history. First asset goal is 10,500, holding 10,000.
        view = self.close_view()
        view.update(
            available_cash="130000",
            accounting_cash="130000",
            market_value="10000",
            economic_equity="140000",
            positions=[dict(security=A, quantity=2000, sellable=2000, blocked=False)],
        )
        plan = build_plan(self.inputs, state, view, NOW)
        self.assertEqual(plan["sides"][A], "HOLD")
        view.update(available_cash="130001", accounting_cash="130001", economic_equity="140001")
        self.assertEqual(build_plan(self.inputs, state, view, NOW)["sides"][A], "BUY")
        view["dividend_receivable"] = "20"
        plan = build_plan(self.inputs, state, view, NOW)
        self.assertEqual(D(plan["reference_nav"]), D("140021"))
        returns = pd.read_csv(self.inputs / "returns.csv", index_col=0)
        returns.iloc[:-1].to_csv(self.inputs / "returns.csv")
        with self.assertRaisesRegex(ValueError, "signal inputs"):
            build_plan(self.inputs, state, view, NOW)
