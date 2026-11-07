import numpy as np
import pandas as pd
from .config import BASE, KEYS

def covariance(history):
    if tuple(history.columns)!=KEYS or len(history)!=63 or not history.index.is_monotonic_increasing or not history.index.is_unique:
        raise ValueError("Expected 63 ordered sessions and ten ordered assets")
    values=history.to_numpy(float)
    if not np.isfinite(values).all() or (values<=-1).any():
        raise ValueError("Invalid total returns")
    cov=np.cov(values.T,ddof=1)
    return .75*cov+.25*np.diag(cov.diagonal())

def target(history):
    cov=covariance(history)
    b=np.asarray(BASE)
    vol=float(np.sqrt(252*b@cov@b))
    if not np.isfinite(vol) or vol<1e-12:
        raise ValueError("Degenerate risk window")
    scale=min(1.,.12/vol)
    return dict(weights=dict(zip(KEYS,(b*scale).tolist())),cash_weight=1-scale,scale=scale,predicted_vol=vol)
