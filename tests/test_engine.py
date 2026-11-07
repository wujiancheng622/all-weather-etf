import json
import tempfile
import unittest
from pathlib import Path
from copy import deepcopy
from v12.events import Paper,remaining
from v12.config import BASE,KEYS,CODES
from v12.store import StateConflict

DAY="2026-11-04"; A=CODES["csi300"]; US=CODES["nasdaq"]
CFG=dict(calendar=["2026-11-03",DAY,"2026-11-05"],start=DAY)
def time(s):return DAY+"T"+s+"+08:00"
def start(day=DAY,ref="2026-11-03"):
    return dict(id=day+"-start",type="start",at=day+"T09:00:00+08:00",reference_date=ref,prices={c:10. for c in CODES.values()},weights=dict(zip(KEYS,[v*.8 for v in BASE])),cash_target=.2)
def status(sec,code=A,value="TRADABLE"):
    at=time(sec)
    return dict(type="status",id=code+sec,at=at,code=code,status=value,effective_at=at)
def quote(sec,code=A,qty=100):
    at=time(sec)
    return dict(id=code+"q"+sec,source_id=sec,type="quote",at=at,exchange_time=at,code=code,bid=9.99,ask=10.,bid_size=qty,ask_size=qty,phase="continuous",unit="shares",buy_blocked=False,sell_blocked=False)
def fixture():
    old=quote("09:30:02")
    old.update(id="late",at=time("10:30:01"))
    q=quote("10:30:02",qty=45)
    return [start(),status("09:30:00"),quote("09:30:01"),status("09:30:03",value="SUSPENDED"),
            status("10:30:00"),old,q,dict(q,id="repeat",at=time("10:30:03")),quote("10:30:04",qty=55),
            status("10:30:05",US),quote("10:30:06",US),
            dict(type="close",id="close",at=time("15:10:00"),prices={c:10. for c in CODES.values()})]

class Events(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/"state.db"
        self.p=Paper(self.path,CFG)
    def tearDown(self):
        self.p.db.close();self.tmp.cleanup()
    def restart(self,cfg=CFG):
        self.p.db.close();self.p=Paper(self.path,cfg,True)
    def test_late_partial_restart(self):
        f=fixture()
        for e in f[:6]:self.p.apply(e)
        self.assertEqual(self.p.state["fills"],[])
        self.p.apply(f[6]);self.restart()
        for e in f[7:]:self.p.apply(e)
        self.assertEqual([v["qty"] for v in self.p.state["fills"]],[45,55])
        self.assertEqual(self.p.state["book"]["cash"],498995.)
        self.assertEqual(self.p.state["book"]["previous_nav"],499995.)
        self.assertEqual(self.p.state["session"]["reasons"][US],"POLICY_NOT_CONFIGURED")
        self.assertEqual(self.p.apply(f[-1]),"DUPLICATE")
        with self.assertRaises(StateConflict):self.p.apply(dict(f[-1],prices={}))
    def test_rounding(self):
        self.assertEqual(remaining(61000,130,500,-1),0)
        self.assertEqual(remaining(94000,9,10000,-1),0)
        self.assertEqual(remaining(0,10,107,-1),107)
    def test_unknown_across_days(self):
        for e in fixture()[:3]:self.p.apply(e)
        oid=next(iter(self.p.state["orders"]))
        self.p.apply(dict(id="?",type="order_status",at=time("09:31:00"),order_id=oid,status="UNKNOWN"))
        self.p.apply(fixture()[-1]);self.restart()
        self.p.apply(start("2026-11-05",DAY))
        for event in [status("09:30:00"),quote("09:30:01")]:
            e=json.loads(json.dumps(event).replace(DAY,"2026-11-05"));e["id"]+="day2";self.p.apply(e)
        self.assertEqual(len(self.p.state["orders"]),1)
        self.assertEqual(self.p.state["session"]["reasons"][A],"ORDER_UNRESOLVED")
    def test_full_cash_floor(self):
        self.p.apply(start())
        for i,code in enumerate(CODES.values()):
            self.p.apply(status("09:30:00",code))
        for sec in ["09:30:01","09:30:02"]:
            for code in CODES.values():self.p.apply(quote(sec,code,100000))
        self.assertGreaterEqual(self.p.state["book"]["cash"],100000-1e-8)
        self.assertEqual(len(self.p.state["fills"]),8)
    def test_premium(self):
        self.p.db.close();cfg=dict(CFG,max_buy_premium=.03)
        self.p=Paper(self.path.parent/"other.db",cfg)
        self.p.apply(start());self.p.apply(status("09:30:00",US))
        def valued(sec,low):
            q=quote(sec,US)
            q["fair_value"]=dict(quality="VALID",code=US,currency="CNY",basis="current_share",low=low,high=low+.1,valued_at=time("09:30:00"),published_at=time("09:30:01"),valid_until=time("10:00:00"))
            return q
        self.p.apply(valued("09:30:02",9))
        self.assertFalse(self.p.state["orders"])
        self.p.apply(valued("09:30:03",10))
        self.p.apply(valued("09:30:04",10))
        self.assertEqual(self.p.state["fills"][0]["code"],US)
    def test_rollback_and_writer(self):
        other=Paper(self.path,CFG,True)
        try:
            self.p.apply(start())
            with self.assertRaises(StateConflict):other.apply(dict(start(),id="other"))
            with self.assertRaises(ValueError):self.p.apply(status("09:30:00","513300.SH"))
            self.assertEqual(self.p.version,1)
        finally:other.db.close()
    def test_corporate_actions(self):
        data=deepcopy(CFG)
        data["dividends"]=[dict(key="csi300",event_id="d",record_date=DAY,ex_date="2026-11-05",payment_date="2026-11-05",cash_per_actual_share=.1)]
        self.p.db.close();self.path=self.path.parent/"corp.db";self.p=Paper(self.path,data)
        for e in fixture():self.p.apply(e)
        self.restart(data)
        self.p.apply(start("2026-11-05",DAY))
        self.assertEqual(self.p.state["book"]["receivables"]["d"],10.)
        self.p.apply(dict(fixture()[-1],id="next_close",at="2026-11-05T15:10:00+08:00"))
        self.assertEqual(self.p.state["book"]["cash"],499005.)

    def test_commit_failure_and_lost_ack(self):
        from unittest.mock import patch
        for e in fixture()[:6]: self.p.apply(e)
        e=fixture()[6]
        original=self.p.db.commit
        with patch.object(self.p.db,'commit',lambda *args:original(*args,fail_before_commit=True)):
            with self.assertRaises(RuntimeError):self.p.apply(e)
        self.restart()
        self.assertFalse(self.p.state["fills"])
        original=self.p.db.commit
        def lost_ack(*args):
            original(*args)
            raise RuntimeError("lost ack")
        with patch.object(self.p.db,'commit',lost_ack):
            with self.assertRaises(RuntimeError):self.p.apply(e)
        self.restart()
        self.assertEqual(self.p.apply(e),"DUPLICATE")
        self.assertEqual(len(self.p.state["fills"]),1)
        self.assertEqual(self.p.state["book"]["cash"],499545.)

    def test_unknown_sell_does_not_freeze_funded_buy(self):
        for e in [start(),status("09:30:00"),quote("09:30:01"),quote("09:30:02",qty=5000)]:self.p.apply(e)
        self.p.apply(fixture()[-1])
        next_day="2026-11-05"
        s=start(next_day,DAY)
        s.update(weights={k:v*.2 for k,v in zip(KEYS,BASE)},cash_target=.8)
        self.p.apply(s)
        def nxt(e):
            e=json.loads(json.dumps(e).replace(DAY,next_day));e['id']+='d2';return e
        for code in (A,CODES['gold']):
            self.p.apply(nxt(status("09:30:00",code)))
        self.p.apply(nxt(quote("09:30:01",A)))
        sell=next(o for o in self.p.state['orders'].values() if o['side']==-1)
        self.p.apply(nxt(dict(type="order_status",id="unknown",status="UNKNOWN",order_id=sell['id'],at=time("09:30:01"))))
        before=self.p.state['book']['cash']
        self.p.apply(nxt(quote("09:30:01",CODES['gold'])))
        self.p.apply(nxt(quote("09:30:02",CODES['gold'])))
        self.assertEqual(self.p.state['book']['cash'],before-1005)
        self.assertEqual(self.p.state['orders'][sell['id']]['filled'],0)
