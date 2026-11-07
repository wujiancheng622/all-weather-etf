from pathlib import Path
from types import SimpleNamespace
import json
import numpy as np
import pandas as pd
from .account import Book
from .config import CONFIG,KEYS,CODES,BASE,digest
from .daily import replay_day
from .risk import target

def load(root):
    inp=root/'p75_risk15_codex_20261106/input'
    source=root/'review_runs/20261105_p75_long_horizon_v1/extended_input'
    raw=pd.read_csv(source/'input/normalized_ohlcv.csv')
    fields=['open','close','high','low','volume']
    panels={f:raw.pivot(index='date',columns='key',values=f)[list(KEYS[:8])].sort_index() for f in fields}
    splits=pd.read_csv(source/'events/split_events.csv').to_dict('records')
    if len({(e['key'],e['effective_date']) for e in splits})!=len(splits):raise ValueError("Duplicate split before share-basis conversion")
    for ev in splits:
        for f in fields[:-1]:
            panels[f].loc[panels[f].index<ev['effective_date'],ev['key']]*=ev['new_shares_per_old_share']
    overseas=[dict(key='nasdaq',effective_date='2022-01-13',new_shares_per_old_share=5),
              dict(key='sp500',effective_date='2022-03-29',new_shares_per_old_share=2)]
    for ev in overseas:
        k=ev['key']
        df=pd.read_csv(inp/'global'/f'{k}_processed.csv',index_col=0)
        for f in fields:
            panels[f][k]=df[f].reindex(panels[f].index)
    for field,table in panels.items():
        values=table.to_numpy()
        if not np.isfinite(values).all() or (values<0 if field=='volume' else values<=0).any():
            raise ValueError("Invalid market data: "+field)
    if (panels['high']<panels['open']).any().any() or (panels['high']<panels['close']).any().any() or (panels['low']>panels['open']).any().any() or (panels['low']>panels['close']).any().any():
        raise ValueError("OHLC range inconsistency")
    splits+=overseas
    dividends=pd.read_csv(source/'events/dividend_events.csv').to_dict('records')
    for events,date in [(splits,'effective_date'),(dividends,'ex_date')]:
        seen=set()
        for e in events:
            ident=(e['key'],e[date])
            if ident in seen: raise ValueError("Duplicate corporate action")
            seen.add(ident)
            e['event_id']=digest(ident)
    returns=pd.read_csv(inp/'global_actual_returns.csv',index_col=0)
    if not returns.index.equals(panels['close'].index[1:]) or tuple(returns.columns)!=KEYS:
        raise ValueError("Non-aligned returns and market calendar")
    adv=panels['volume'].shift().rolling(20).mean()*100
    for e in overseas:
        k=e['key']; at=e['effective_date']; pos=adv.index.get_loc(at)
        for j in range(pos,pos+20):
            v=panels['volume'][k].iloc[j-20:j].copy()*100
            v.loc[v.index<at]*=e['new_shares_per_old_share']
            adv.loc[adv.index[j],k]=v.mean()
    return SimpleNamespace(panels=panels,returns=returns,dividends=dividends,splits=splits),adv

def run(root,out):
    data,adv=load(root)
    start='2020-07-30'
    dates=data.returns.loc[start:'2026-11-04'].index
    book=Book.new(500000.,start)
    outputs={k:[] for k in ('ledger','weights','holdings','trades','plans','events','targets')}
    for d in dates:
        i=data.returns.index.get_loc(d)
        sig=target(data.returns.iloc[i-63:i])
        outputs['targets'].append(dict(date=d,**sig['weights'],cash_target=sig['cash_weight']))
        result=replay_day(book,d,np.array(list(sig['weights'].values())),data,CONFIG,'constrained_open_proxy',adv)
        for k,v in result.items():
            outputs[k].extend(v if isinstance(v,list) else [v])
    out.mkdir(parents=True,exist_ok=True)
    oracle=root/'review_runs/20261106_cross_model_synthesis_v1/results/accounts/S_US40_V12_9bp'
    checks={}
    for k,rows in outputs.items():
        df=pd.DataFrame(rows)
        df.to_csv(out/f'{k}.csv',index=False)
        if k not in ('ledger','targets','weights','trades'): continue
        ref=pd.read_csv(oracle/f'{k}.csv')
        assert len(ref)==len(df)
        for col in ref:
            if pd.api.types.is_numeric_dtype(ref[col]):
                error=float(abs(df[col]-ref[col]).max())
                tol=0 if col in ('shares','signed_shares','trade_count') else (1e-10 if k in ('weights','targets') else 1e-5)
                if error>tol or not np.isfinite(error): raise ValueError((k,col,error))
                checks[f'{k}.{col}']=error
            else: pd.testing.assert_series_equal(df[col],ref[col],check_names=False)
    assert len(dates)==1518 and len(outputs['trades'])==739
    reference_weights=pd.read_csv(oracle/'weights.csv',index_col='date').loc[dates,list(KEYS)]
    reference_nav=pd.read_csv(oracle/'ledger.csv',index_col='date').nav.loc[dates]
    reference_shares=reference_weights.mul(reference_nav,axis=0)/data.panels['close'].loc[dates,list(KEYS)]
    np.testing.assert_allclose(reference_shares,pd.DataFrame(outputs['holdings']).set_index('date'),atol=1e-7,rtol=0)
    (out/'validation.json').write_text(json.dumps(dict(days=len(dates),trades=len(outputs['trades']),end_nav=book.previous_nav,errors=checks),indent=2))
    print('PASS',len(dates),len(outputs['trades']),book.previous_nav)
