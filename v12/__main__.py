import argparse
import json
from pathlib import Path
from datetime import datetime
import pandas as pd
from .config import CODES,KEYS,CONFIG,BASE
from .baseline import run
from .events import Paper,CN,stamp
from .risk import target
from .observe import poll,tick

def dump(file,data):
    file.write_text(json.dumps(data,ensure_ascii=False,indent=2,allow_nan=False),encoding="utf8")

def main():
    parser=argparse.ArgumentParser()
    commands=parser.add_subparsers(dest="cmd",required=True)
    baseline=commands.add_parser("baseline_replay")
    baseline.add_argument("--data-root",type=Path,required=True)
    baseline.add_argument("--output",type=Path,required=True)
    probe=commands.add_parser("market_probe")
    prep=commands.add_parser("prepare_session")
    for key in ("data-root","calendar","output"):
        prep.add_argument("--"+key,type=Path,required=True)
    prep.add_argument("--date",required=True)
    prep.add_argument("--start-day")
    probe.add_argument("--host",default="127.0.0.1")
    probe.add_argument("--code",choices=CODES.values(),default=CODES["nasdaq"])
    for mode in ("event_replay","paper_observe"):
        command=commands.add_parser(mode)
        command.add_argument("--settings",type=Path,required=True)
        command.add_argument("--output",type=Path,required=True)
        command.add_argument("--resume",action="store_true")
        if mode=="event_replay":command.add_argument("--events",type=Path,required=True)
        else:
            command.add_argument("--session",type=Path,required=True)
            command.add_argument("--host",default="127.0.0.1")
    a=parser.parse_args()
    if a.cmd=="prepare_session":
        from .prepare import prepare
        prepare(a.data_root,a.date,a.calendar,a.output,a.start_day);return
    if a.cmd=="market_probe":
        print(json.dumps(tick(a.host,a.code),ensure_ascii=False));return
    if a.cmd=="baseline_replay":
        run(a.data_root,a.output)
        dump(a.output/"effective_config.json",dict(mode=a.cmd,codes=CODES,base=BASE,**CONFIG.flat,lookback=63,shrink=.25,vol_target=.12))
        return
    cfg=json.loads(a.settings.read_text(encoding="utf-8-sig"))
    cfg["mode"]=a.cmd
    if a.cmd=="paper_observe" and cfg.get("max_buy_premium") is not None:
        raise ValueError("Configured thresholds are offline diagnostic fixtures only")
    engine=Paper(a.output/"state.sqlite",cfg,a.resume)
    try:
        if a.cmd=="event_replay":
            for line in a.events.read_text(encoding="utf-8-sig").splitlines():
                if line.strip(): engine.apply(json.loads(line))
        else:
            session=json.loads(a.session.read_text(encoding="utf-8-sig"))
            day=session["date"]; now=datetime.now(CN)
            if str(now.date())!=day:raise ValueError("Market observer requires today's prepared session")
            dates=cfg["calendar"]; idx=dates.index(day)
            history=pd.read_csv(a.session.parent/session["returns_csv"],index_col=0)
            history=history.loc[history.index<day].tail(63)
            if idx<63 or list(history.index)!=dates[idx-63:idx] or session["reference_date"]!=dates[idx-1] or stamp(session["as_of"])>now:
                raise ValueError("Missing point-in-time signal history")
            sig=target(history)
            for update in session.get("confirmed_order_events",[]):
                if update.get("type")!="order_status" or stamp(update["at"])>datetime.now(CN):
                    raise ValueError("Expected confirmed paper order status")
                engine.apply(update)
            if engine.state["session"].get("date")!=day:
                engine.apply(dict(type="start",at=datetime.now(CN).isoformat(),id=day+"/start",reference_date=dates[idx-1],
                    prices=session["prices"],weights=sig["weights"],cash_target=sig["cash_weight"]))
            elif abs(engine.state["session"]["cash_target"]-sig["cash_weight"])>1e-12:
                raise ValueError("Current session already has a different frozen target")
            observations=[] if engine.state["session"]["closed"] else poll(engine,a.host,session.get("contracts",{}))
            dump(a.output/"observations.json",observations)
            marks=engine.state["session"]["marks"]
            from .account import Book
            from .risk import covariance
            import numpy as np
            book=Book.restore(engine.state["book"])
            prices=engine.prices(marks)
            nav=book.nav(prices)
            actual=np.array(book.holdings)*prices/nav
            dump(a.output/"risk.json",dict(target=sig,actual=dict(zip(CODES.values(),actual.tolist())),
                actual_vol=float(np.sqrt(252*actual@covariance(history)@actual)),cash=book.cash,
                marked_nav=nav,actual_cash_weight=book.cash/nav,
                qdii_shortfall=sum(sig["weights"][k]-actual[KEYS.index(k)] for k in ("nasdaq","sp500")),
                reasons={code:engine.state["session"]["reasons"].get(code, "POLICY_NOT_CONFIGURED" if k in ("nasdaq","sp500") else "WAIT_MARKET") for k,code in CODES.items()}))
            if session.get("close_prices") and now.hour>=15 and not engine.state["session"]["closed"]:
                engine.apply(dict(type="close",at=datetime.now(CN).isoformat(),id=day+"/close",prices=session["close_prices"]))
        dump(a.output/"effective_config.json",dict(settings=cfg,mapping=CODES,disabled_candidates=["513300.SH"],base=BASE,window=63,shrink=.25,vol_target=.12))
        dump(a.output/"state.json",engine.state)
        for key in ("orders","fills","ledger"):
            rows=engine.state[key]
            pd.DataFrame(list(rows.values()) if isinstance(rows,dict) else rows).to_csv(a.output/(key+".csv"),index=False)
        print(a.cmd,"version",engine.version,"fills",len(engine.state["fills"]))
    finally: engine.db.close()

if __name__=="__main__": main()
