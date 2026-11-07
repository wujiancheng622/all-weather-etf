"""Prepare portable input files for one observation day, reading local retained data."""
from pathlib import Path
import json
import pandas as pd
from .baseline import load
from .config import CODES
from .sessions import checked_calendar

def prepare(root,day,calendar_file,out,start_day=None):
    data,_=load(root)
    calendar=checked_calendar(Path(calendar_file).read_text(encoding="utf-8-sig").splitlines())
    idx=calendar.index(day)
    if idx<63:raise ValueError("Calendar must include 63 prior sessions")
    previous=calendar[idx-1]
    returns=data.returns.loc[:previous].tail(63)
    if list(returns.index)!=calendar[idx-63:idx]:raise ValueError("Refresh the local return and price inputs through the reference close")
    source=data.panels['close'].loc[previous]
    out.mkdir(exist_ok=False,parents=True)
    returns.to_csv(out/'returns.csv')
    actions=[e for e in data.splits if e['effective_date'] in calendar]
    divs=[{k:e[k] for k in ('key','event_id','record_date','ex_date','payment_date','cash_per_actual_share')}
          for e in data.dividends if any(e[k] in calendar for k in ('record_date','ex_date','payment_date'))]
    for e in actions:
        if e.get("fractional_share_rule","").startswith("round_up_to_integer"):e["rounding"]="ceil"
    settings=dict(start=start_day or day,calendar=calendar,max_buy_premium=None,dividends=divs,splits=actions)
    session=dict(date=day,reference_date=previous,as_of=previous+"T15:00:00+08:00",returns_csv="returns.csv",
                 prices={code:float(source[k]) for k,code in CODES.items()},contracts={})
    for file,value in [('settings',settings),('session',session)]:
        (out/(file+'.json')).write_text(json.dumps(value,indent=2,allow_nan=False),encoding='utf8')
