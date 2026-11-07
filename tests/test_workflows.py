import io
import sys
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
import numpy as np
import pandas as pd
from v12.__main__ import main
from v12.config import KEYS,BASE,CODES
from v12.events import Paper,CN
from v12.risk import target
from v12.observe import poll
from test_engine import CFG,DAY,A,US,fixture,start

class Workflows(unittest.TestCase):
    def test_risk(self):
        x=np.tile([-.03,.03],32)[:63]
        panel=pd.DataFrame(np.outer(x,np.ones(10)),columns=KEYS)
        sig=target(panel)
        expected=x.std(ddof=1)*np.sqrt(252*(.75+.25*np.dot(BASE,BASE)))
        self.assertAlmostEqual(sig["predicted_vol"],expected)
        self.assertAlmostEqual(sum(sig["weights"].values()),.12/expected)
        with self.assertRaises(ValueError):target(panel.iloc[:62])
        with self.assertRaises(ValueError):target(panel.iloc[:,::-1])
        with self.assertRaises(ValueError):target(panel*0)
    def test_fixture_capital_flow(self):
        with tempfile.TemporaryDirectory() as directory:
            p=Paper(Path(directory)/'state',dict(CFG,mode="event_replay",fixture_flows=True))
            try:
                p.apply(start());p.apply(fixture()[-1])
                p.apply(dict(type="capital_flow",id="transfer",owner="V12",asset=A,amount=100,at=DAY+"T16:00:00+08:00"))
                self.assertEqual(p.state["book"]["previous_nav"],501000.)
                self.assertEqual(p.state["units"],501000.)
            finally:p.db.close()
    def test_exact_buffer_and_negative_input(self):
        from v12.account import plan_quantities
        from v12.config import CONFIG
        h=np.array([2000,0]);prices=np.array([10.,10.])
        self.assertEqual(plan_quantities(h,20000.,0.,prices,np.array([.525,0.]),CONFIG.flat)[0],0)
        self.assertEqual(plan_quantities(h,20000.,0.,prices,np.array([.52501,0.]),CONFIG.flat)[0],100)
    def test_command_with_scripted_market(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); output=root/'out'
            dates=pd.bdate_range(end='2026-11-04',periods=64).strftime('%Y-%m-%d').tolist()
            pd.DataFrame(np.random.default_rng(1).normal(0,.006,(63,10)),index=dates[:-1],columns=KEYS).to_csv(root/'r.csv')
            (root/'settings.json').write_text(json.dumps(dict(start=DAY,calendar=dates)))
            contracts={c:dict(source="fixture",valid_date=DAY,status_map={"1":"TRADABLE"},depth_multiplier=100,upper=11,lower=9) for c in CODES.values()}
            session=dict(date=DAY,reference_date=dates[-2],as_of=dates[-2]+"T15:00:00+08:00",returns_csv="r.csv",contracts=contracts,prices={c:10. for c in CODES.values()})
            (root/'day.json').write_text(json.dumps(session))
            class Clock(datetime):
                at=datetime.fromisoformat(DAY+"T10:00:00+08:00")
                @classmethod
                def now(cls,tz=None):return cls.at
            def fake(host,code):
                return dict(time=int(Clock.at.timestamp()*1000),stockStatus=1,bidPrice=[9.99],askPrice=[10.],askVol=[2],bidVol=[2])
            args=['v12','paper_observe','--settings',str(root/'settings.json'),'--session',str(root/'day.json'),'--output',str(output)]
            with patch('v12.__main__.datetime',Clock),patch('v12.observe.datetime',Clock),patch('v12.__main__.poll',lambda e,h,c:poll(e,h,c,fake)),patch('sys.stdout',new_callable=io.StringIO):
                with patch.object(sys,'argv',args):main()
                Clock.at=Clock.at.replace(second=1)
                with patch.object(sys,'argv',args+['--resume']):main()
            state=json.loads((output/'state.json').read_text())
            self.assertEqual(len(state['fills']),8)
            self.assertEqual(state['session']['reasons'][US],"POLICY_NOT_CONFIGURED")
            self.assertGreater(json.loads((output/'risk.json').read_text())['qdii_shortfall'],0)
    def test_split_restart(self):
        settings=dict(CFG,splits=[dict(key='csi300',effective_date='2026-11-05',event_id='s',new_shares_per_old_share=2)])
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'state'
            p=Paper(path,settings)
            try:
                for e in fixture()[:-1]:p.apply(e)
                oid=next(iter(p.state['orders']))
                p.apply(fixture()[-1])
            finally:p.db.close()
            p=Paper(path,settings,True)
            try:
                with self.assertRaises(ValueError):p.apply(start('2026-11-05',DAY))
                p.apply(dict(type='order_status',id='cancel',status='CANCELLED',order_id=oid,at=DAY+'T16:00:00+08:00'))
                s=start('2026-11-05',DAY);p.apply(s);p.apply(s)
                self.assertEqual(p.state['book']['holdings'][0],200)
                self.assertEqual(p.state['session']['marks'][A],5.)
            finally:p.db.close()
