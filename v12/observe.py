"""One GET per security, fixed read-only port and route, no redirects."""
import http.client
import json
from datetime import datetime
from .config import CODES
from .events import CN,stamp

def tick(host,code):
    if code not in CODES.values() or any(x in host for x in ("/","@",":","?","#")): raise ValueError("Expected market host name or IPv4")
    conn=http.client.HTTPConnection(host,8002,timeout=4)
    try:
        conn.request("GET","/api/v1/tick?stock_code="+code)
        response=conn.getresponse()
        if response.status!=200: raise ValueError(f"Market HTTP {response.status}")
        raw=json.loads(response.read(1_000_000))
        return raw
    finally: conn.close()

def poll(engine,host,contracts,get_tick=tick):
    observations=[]
    for code in CODES.values():
        try:
            raw=get_tick(host,code)
        except (OSError,http.client.HTTPException,ValueError) as exc:
            observations.append(dict(code=code,reason="MARKET_READ_FAILED",error=str(exc)))
            continue
        now=datetime.now(CN)
        record=dict(code=code,received_at=now.isoformat(),snapshot=raw)
        observations.append(record)
        rule=contracts.get(code,{})
        if not raw or not raw.get("time") or not rule:
            record["reason"]="UNKNOWN_MARKET_CONTRACT";continue
        qt=datetime.fromtimestamp(raw["time"]/1000,CN)
        if engine.state["session"].get("date")!=str(now.date()) or not 0<=(now-qt).total_seconds()<=5:
            record["reason"]="STALE_OR_CLOSED";continue
        status=rule.get("status_map",{}).get(str(raw.get("stockStatus")),"UNKNOWN")
        if not rule.get("source") or not rule.get("valid_date")==str(now.date()): status="UNKNOWN"
        at=now.isoformat()
        prefix=f"{code}/{raw['time']}"
        engine.apply(dict(type="status",code=code,status=status,effective_at=qt.isoformat(),at=at,id=prefix+"/status/"+at))
        if status!="TRADABLE" or rule.get("depth_multiplier") not in (1,100):
            record["reason"]="UNKNOWN_OR_UNTRADABLE";continue
        try:
            bid,ask=raw["bidPrice"][0],raw["askPrice"][0]
            bv,av=raw["bidVol"][0],raw["askVol"][0]
            if not 0<bid<=ask or min(bv,av)<0 or not all(float(v).is_integer() for v in (bv,av)):
                raise ValueError()
        except (KeyError,IndexError,TypeError,ValueError):
            record["reason"]="UNKNOWN_BOOK";continue
        engine.apply(dict(type="quote",at=at,id=prefix+"/receive/"+at,source_id=prefix,
            exchange_time=qt.isoformat(),code=code,bid=bid,ask=ask,bid_size=int(bv)*rule["depth_multiplier"],
            ask_size=int(av)*rule["depth_multiplier"],phase="continuous",unit="shares",
            buy_blocked=ask>=rule["upper"] if rule.get("upper") else None,
            sell_blocked=bid<=rule["lower"] if rule.get("lower") else None))
        record["reason"]=engine.state["session"]["reasons"].get(code,"NO_INTENT")
    return observations
