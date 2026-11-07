"""Local paper account. Every accepted event commits one complete state transition."""
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import math
import numpy as np
from .account import Book
from .config import KEYS, CODES, digest
from .sessions import require_next_session, checked_calendar
from .store import Journal

CN=timezone(timedelta(hours=8))
LIVE={"OPEN","PARTIAL","UNKNOWN","CANCEL_REQUESTED"}
def stamp(value):
    instant=datetime.fromisoformat(value)
    if instant.utcoffset() is None: raise ValueError("Timezone required")
    return instant.astimezone(CN)
def number(x,positive=False):
    if isinstance(x,bool) or not math.isfinite(x) or (x<=0 if positive else x<0):
        raise ValueError("Invalid numeric value")
    return float(x)
def commission(amount):
    return max(5.,amount*.0001) if amount>0 else 0.
def pending(o):
    return o["quantity"]-o["filled"]
def reservation(o):
    n=pending(o)*o["limit"]
    return n+commission(o["notional"]+n)-o["fees"]
def remaining(goal,price,shares,side,inflight=0):
    difference=goal/price-shares
    qty=max(0.,side*difference-inflight)
    if goal==0 and side==-1: return int(qty)
    return 100*math.floor(qty/100+1e-12)

class Paper:
    def __init__(self,path,settings,resume=False):
        self.settings=deepcopy(settings)
        self.calendar=checked_calendar(settings["calendar"])
        self.limit=settings.get("max_buy_premium")
        if self.limit is not None: number(self.limit)
        self.div=settings.get("dividends",[])
        self.splits=settings.get("splits",[])
        for events,day in [(self.div,"ex_date"),(self.splits,"effective_date")]:
            ids=[(e["key"],e[day]) for e in events]
            if len(ids)!=len(set(ids)) or len({e["event_id"] for e in events})!=len(ids): raise ValueError("Duplicate action")
            for e in events:
                if e["key"] not in KEYS or e[day] not in self.calendar: raise ValueError("Invalid action")
                if day=="ex_date":
                    if not e["record_date"]<e["ex_date"]<=e["payment_date"] or any(e[k] not in self.calendar for k in ("record_date","payment_date")): raise ValueError("Dividend calendar")
                    number(e["cash_per_actual_share"])
                else:
                    number(e["new_shares_per_old_share"],True)
                    if e.get("rounding")!="ceil" and not float(e["new_shares_per_old_share"]).is_integer(): raise ValueError("Explicit supported split rounding required")
        fixed={k:v for k,v in settings.items() if k not in ("calendar","dividends","splits")}
        self.db=Journal(path,{"schema":"v12-paper-2","settings":digest(fixed),"mapping":CODES})
        try:
            if not resume:
                self.db.initialize(dict(book=Book.new(500000.,settings["start"]).export(),units=500000.,flows=[],session={},orders={},fills=[],ledger=[],last_at=None,stopped=False,inputs=self.inputs()))
            self.version,self.state=self.db.read()
            old=self.state["inputs"]
            if self.calendar[:len(old["calendar"])]!=old["calendar"]:raise ValueError("Calendar history changed")
            day=self.state["session"].get("date", "")
            for key,date in (("dividends","record_date"),("splits","effective_date")):
                current=self.inputs()[key]
                if any(e not in current for e in old[key]) or any(e[date]<=day for e in current if e not in old[key]):
                    raise ValueError("Corporate action history changed")
        except BaseException:
            self.db.close()
            raise

    def inputs(self):
        return dict(calendar=self.calendar,dividends=self.div,splits=self.splits)

    def apply(self,e):
        if self.db.seen(e["id"],e):
            self.version,self.state=self.db.read()
            return "DUPLICATE"
        s=deepcopy(self.state)
        now=stamp(e["at"])
        if s["last_at"] and now<stamp(s["last_at"]): raise ValueError("Event clock moved backwards")
        book=Book.restore(s["book"])
        session=s["session"]
        kind=e["type"]
        if kind=="start":
            self.start(s,e,book)
        elif kind=="capital_flow":
            if self.settings.get("mode")!="event_replay" or not self.settings.get("fixture_flows") or e.get("owner")!="V12":
                raise ValueError("An explicit owned offline fixture is required")
            if not session.get("closed") or str(now.date())!=session["date"] or any(o["status"] in LIVE for o in s["orders"].values()):
                raise ValueError("Flows require a settled closing mark")
            unit_nav=book.previous_nav/s["units"]
            delta=e["amount"]
            if isinstance(delta,bool) or not math.isfinite(delta): raise ValueError("Invalid capital flow")
            if e["asset"]=="cash": book.cash+=delta
            else:
                i=list(CODES.values()).index(e["asset"])
                if type(delta) is not int: raise ValueError("Integer units required")
                book.holdings[i]+=delta
                delta*=session["marks"][e["asset"]]
            book.previous_nav+=delta;s["units"]+=delta/unit_nav
            if s["units"]<=0: raise ValueError("Nonpositive account units")
            s["flows"].append(dict(id=e["id"],amount=delta,unit_nav=unit_nav))
        elif kind=="stop":
            s["stopped"]=True
        elif kind=="order_status":
            # Terminal confirmations may arrive after close and before the next
            # session's corporate actions. Preserve reserves until confirmation.
            order=s["orders"][e["order_id"]]
            status=e["status"]
            if order["status"] not in LIVE or status not in ("UNKNOWN","CANCEL_REQUESTED","CANCELLED","EXPIRED","REJECTED"):
                raise ValueError("Invalid order transition")
            order["status"]=status
        else:
            if not session or session["closed"] or str(now.date())!=session["date"]: raise ValueError("No matching session")
            if kind=="quote": self.quote(s,e,book)
            elif kind=="status":
                code=e["code"]; status=e["status"]
                if code not in CODES.values() or status not in ("TRADABLE","SUSPENDED","UNKNOWN","CLOSED"): raise ValueError("Invalid status")
                effective=stamp(e["effective_at"])
                old=session["status"].get(code)
                last=session.setdefault("status_asof",{}).get(code)
                if effective>now or effective.date()!=now.date() or (last and (effective<stamp(last) or (effective==stamp(last) and old[0]!=status))): raise ValueError("Status time")
                session["status_asof"][code]=e["effective_at"]
                if not old or old[0]!=status: session["status"][code]=[status,e["effective_at"]]
            elif kind=="close":
                if now.hour<15: raise ValueError("Closing mark too early")
                prices=self.prices(e["prices"])
                session["marks"]=dict(zip(CODES.values(),prices))
                session["actions"]+=book.day_close(session["date"],prices,self.div)
                session["closed"]=True
                s["ledger"].append(dict(date=session["date"],nav=book.previous_nav,unit_nav=book.previous_nav/s["units"],cash=book.cash,receivable=sum(book.receivables.values()),holdings=dict(zip(CODES.values(),book.holdings)),actions=session["actions"],targets=session["goal"],cash_target=session["cash_target"],reasons=session["reasons"].copy()))
            else: raise ValueError("Unsupported event")
        book.validate()
        open_orders=[o for o in s["orders"].values() if o["status"] in LIVE]
        if sum(reservation(o) for o in open_orders if o["side"]==1)>book.cash+1e-6: raise ValueError("Cash reservation exceeds cash")
        for i,code in enumerate(CODES.values()):
            if sum(pending(o) for o in open_orders if o["side"]==-1 and o["code"]==code)>book.holdings[i]: raise ValueError("Sell reservation exceeds shares")
        s["inputs"]=self.inputs()
        s["book"]=book.export(); s["last_at"]=e["at"]
        self.version=self.db.commit(self.version,e["id"],e,s)
        self.state=s
        return "APPLIED"

    @staticmethod
    def prices(prices):
        if set(prices)!=set(CODES.values()): raise ValueError("Expected ten security codes")
        return [number(prices[c],True) for c in CODES.values()]

    def start(self,s,e,b):
        day=str(stamp(e["at"]).date()); prev=e["reference_date"]
        require_next_session(self.calendar,prev,day)
        if s["stopped"] or (s["session"] and not s["session"]["closed"]) or (b.last_date!=prev if b.last_date else day!=b.start_date):
            raise ValueError("Invalid session transition")
        prices=self.prices(e["prices"]); w=e["weights"]
        if set(w)!=set(KEYS): raise ValueError("Target mapping")
        from .config import BASE
        cash=number(e["cash_target"])
        if cash>1 or not np.allclose([w[k] for k in KEYS],np.array(BASE)*(1-cash),rtol=0,atol=1e-12):
            raise ValueError("Invalid V12 target")
        nav=b.nav(prices)
        goal={}; sides={}
        for i,(k,code) in enumerate(CODES.items()):
            goal[code]=number(w[k])*nav
            held=b.holdings[i]*prices[i]; delta=goal[code]-held
            sides[code]=(1 if delta>0 else -1) if abs(delta)>held*.05+1e-10 else 0
        for ev in self.splits:
            if ev["effective_date"]==day:
                code=CODES[ev["key"]]
                if any(o["code"]==code and o["status"] in LIVE for o in s["orders"].values()): raise ValueError("Resolve outstanding order before split")
                prices[list(KEYS).index(ev["key"])]/=ev["new_shares_per_old_share"]
        _,actions=b.day_start(day,self.div,self.splits)
        marks=dict(zip(CODES.values(),prices))
        for ev in self.div:
            if ev["ex_date"]==day: marks[CODES[ev["key"]]]-=ev["cash_per_actual_share"]
        self.prices(marks)
        s["session"]=dict(date=day,goal=goal,side=sides,cash_target=cash,marks=marks,
            closed=False,status={},seen={},last_quote={},sellable=dict(zip(CODES.values(),b.holdings)),actions=actions,reasons={})

    def quote(self,s,e,b):
        session=s["session"]; code=e["code"]
        if code not in CODES.values(): raise ValueError("External security")
        reason=session["reasons"]
        state=session["status"].get(code)
        qt=stamp(e["exchange_time"]); now=stamp(e["at"])
        sec=qt.hour*3600+qt.minute*60+qt.second
        if not state or state[0]!="TRADABLE" or qt<stamp(state[1]) or qt.date()!=now.date() or not 0<=(now-qt).total_seconds()<=5 or not (34200<=sec<41400 or 46800<=sec<54000) or e.get("phase")!="continuous":
            reason[code]="WAIT_MARKET"; return
        if not e.get("source_id") or e.get("unit")!="shares": reason[code]="UNKNOWN_DEPTH"; return
        identity=code+"/"+str(e["source_id"])
        signature=digest([e[k] for k in ("exchange_time","bid","ask","bid_size","ask_size")])
        if identity in session["seen"]:
            if session["seen"][identity]!=signature: raise ValueError("Conflicting quote")
            return
        if code in session["last_quote"] and qt<=stamp(session["last_quote"][code]):
            reason[code]="WAIT_NEW_QUOTE"; return
        bid=number(e["bid"],True); ask=number(e["ask"],True)
        if bid>ask or any(type(e[k]) is not int or e[k]<0 for k in ("ask_size","bid_size")): raise ValueError("Invalid book")
        session["seen"][identity]=signature; session["last_quote"][code]=e["exchange_time"]
        session["marks"][code]=(bid+ask)/2
        side=session["side"][code]
        if not side: return
        price=ask if side==1 else bid
        if e.get("buy_blocked" if side==1 else "sell_blocked") is not False:
            reason[code]="PRICE_LIMIT_UNKNOWN_OR_BLOCKED"; return
        if side==1 and code in (CODES["nasdaq"],CODES["sp500"]):
            if self.limit is None: reason[code]="POLICY_NOT_CONFIGURED";return
            fv=e.get("fair_value",{})
            if not (fv.get("quality")=="VALID" and fv.get("code")==code and fv.get("currency")=="CNY" and fv.get("basis")=="current_share" and 0<number(fv["low"])<=number(fv["high"]) and stamp(fv["valued_at"])<=stamp(fv["published_at"])<=now<=stamp(fv["valid_until"])):
                reason[code]="UNKNOWN_VALUE";return
            limits=[o["limit"] for o in s["orders"].values() if o["code"]==code and o["side"]==1 and o["status"] in LIVE]
            if max([price,*limits])/fv["low"]-1>self.limit: reason[code]="PREMIUM_LIMIT";return
        i=list(CODES.values()).index(code)
        active=[o for o in s["orders"].values() if o["status"] in LIVE]
        own=[o for o in active if o["code"]==code]
        need=remaining(session["goal"][code],price,b.holdings[i],side)
        if own:
            o=own[0]
            if o["status"] not in ("OPEN","PARTIAL") or o["day"]!=session["date"] or o["side"]!=side:
                reason[code]="ORDER_UNRESOLVED";return
            if qt<=stamp(o["submitted"]) or (price-o["limit"])*side>1e-12: return
            # A submitted board-lot order may itself fill in odd-sized pieces.
            units=max(0,math.floor(side*(session["goal"][code]/price-b.holdings[i])+1e-10))
            qty=min(pending(o),units,e["ask_size" if side==1 else "bid_size"])
            if side==1:
                free=b.cash-session["cash_target"]*b.nav(self.prices(session["marks"]))-sum(reservation(order) for order in active if order is not o and order["side"]==1)
                # Include the incremental cumulative fee; an affordable partial
                # fill may be smaller than the requested quote depth.
                notional=max(0.,min(free-max(0.,5.-o["fees"]),
                                    (free+o["fees"]-.0001*o["notional"])/1.0001))
                qty=min(qty,int(notional/price+1e-12))
            if qty<=0:return
            value=qty*price; charge=commission(o["notional"]+value)-o["fees"]
            b.fill(tuple(KEYS)[i],side*qty,price,charge)
            if side==-1: session["sellable"][code]-=qty
            o["filled"]+=qty;o["fees"]+=charge;o["notional"]+=value
            o["status"]="PARTIAL" if pending(o) else "FILLED"
            s["fills"].append(dict(execution_id=digest([o["id"],identity]),order_id=o["id"],code=code,qty=side*qty,price=price,fee=charge,at=e["at"],quote_id=identity,simulated=True))
            reason[code]=o["status"];return
        if s["stopped"]: return
        if side==1:
            nav=b.nav(self.prices(session["marks"]))
            budget=b.cash-sum(reservation(o) for o in active if o["side"]==1)-session["cash_target"]*nav
            # Apportion limited cash across remaining buy intentions at current marks.
            requests={}
            for j,c in enumerate(CODES.values()):
                if session["side"][c]!=1: continue
                mark=price if c==code else session["marks"][c]
                working=sum(pending(o) for o in active if o["code"]==c and o["side"]==1)
                amount=remaining(session["goal"][c],mark,b.holdings[j],1,working)*mark
                requests[c]=amount+commission(amount)
            total=sum(requests.values())
            if total>max(0,budget): budget=max(0,budget)*requests.get(code,0)/total
            maximum=max(0,min(int((budget-5)/price),int(budget/(price*1.0001))))//100*100
            need=min(need,maximum)
        else: need=min(need,session["sellable"][code])
        if need<=0:reason[code]="NO_AFFORDABLE_RESIDUAL";return
        oid=digest([e["id"],code,side])
        s["orders"][oid]=dict(id=oid,day=session["date"],code=code,side=side,quantity=need,limit=price,filled=0,notional=0.,fees=0.,status="OPEN",submitted=e["at"])
        reason[code]="OPEN"
