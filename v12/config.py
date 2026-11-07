import hashlib
import json
from types import SimpleNamespace

CODES = dict(zip("csi300 csi1000 divlow bond10 gold metals energy soy nasdaq sp500".split(),
                 "510300.SH 512100.SH 512890.SH 511260.SH 518880.SH 159980.SZ 159981.SZ 159985.SZ 513100.SH 513500.SH".split()))
KEYS = tuple(CODES)
BASE = (.075,.0225,.05,.125,.1625,.0525,.05625,.05625,.2,.2)
def canonical_json(obj):
    return json.dumps(obj,ensure_ascii=False,sort_keys=True,separators=(",",":"),allow_nan=False)
def digest(obj):
    return hashlib.sha256(canonical_json(obj).encode()).hexdigest()
CONFIG = SimpleNamespace(flat=dict(relative_buffer=.05,lot_size=100,minimum_commission=5.,commission_rate=.0001,slippage_rate=.0009),
                         raw={"execution":{"adv_fraction":.01}})
