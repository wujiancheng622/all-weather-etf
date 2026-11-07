"""One explicit authorization, one domestic-ETF logical order (at most 100 shares).

The journal is rooted in this installation, NOT --run-root. Reusing an authorization
in a different output directory therefore only recovers the original request. A
missing/uncertain identity is never permission to submit again. Keep these journals.
"""
from dataclasses import dataclass, replace
from pathlib import Path
import re

from ..config import CODES, digest
from .account import QDII, fee_ceiling, money
from .order_loop import Skip, Stop

AUTH_ROOT = Path(__file__).resolve().parents[2] / "runtime" / "trial_authorizations"


@dataclass(frozen=True)
class TrialAuthorization:
    authorization_id: str
    code: str
    side: str

    def __post_init__(self):
        if not isinstance(self.authorization_id, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", self.authorization_id
        ):
            raise ValueError("An explicit, stable trial authorization ID is required")
        if self.code not in CODES.values() or self.code in QDII:
            raise ValueError("Trial permits only one V12 domestic ETF")
        if self.side not in ("BUY", "SELL"):
            raise ValueError("Trial requires one explicit BUY or SELL direction")

    @property
    def identity(self):
        return dict(authorization_id=self.authorization_id, code=self.code, side=self.side, max_shares=100)

    @property
    def folder(self):
        # Plan, day and scope are bound by StateFile.identity, never by an output path.
        return AUTH_ROOT / digest(["v12", "simulation-8001", self.authorization_id])

    @property
    def request_id(self):
        # Stable even if a journal is unavailable; the gateway's immutable request
        # identity remains a second line of defence, not a recovery permission.
        return "v12:trial:" + digest(["v12", "simulation-8001", self.authorization_id])[:32]

    def select(self, legs):
        selected = [leg for leg in legs if leg.code == self.code and leg.side == self.side]
        if not selected or selected[0].quantity <= 0:
            raise Skip("TRIAL_NOT_TRIGGERED_BY_PLAN")
        # Do not remove the ordinary sell-before-buy prerequisite by filtering legs.
        if self.side == "BUY" and any(leg.side == "SELL" and not leg.deferrable for leg in legs):
            raise Skip("TRIAL_REQUIRES_UNAUTHORIZED_DOMESTIC_SELL")
        return [replace(selected[0], quantity=min(100, selected[0].quantity), max_attempts=1)]

    def make_request(self, planner, leg, attempt, filled=0, remaining=None):
        # Run ALL normal ownership, liquidity, quantity, budget and cash-floor checks
        # before clipping. No extra sale and no forced minimum-sized trade.
        request, facts = planner.make_request(leg, attempt, filled, remaining)
        request = dict(request)
        facts = dict(facts)
        request["volume"] = min(100, request["volume"])
        if request["volume"] <= 0:
            raise Skip("TRIAL_NO_EXECUTABLE_QUANTITY")
        request.update(request_id=self.request_id, order_remark=self.request_id)
        request["fee_ceiling"] = str(fee_ceiling(request["volume"], money(request["limit_price"], "limit")))
        facts["unrestricted_desired_quantity"] = facts["desired_quantity"]
        facts["desired_quantity"] = request["volume"]
        facts["trial_authorization"] = self.identity
        return request, facts

    def check_submit(self, request, state):
        """Final boundary, on every POST including exact-content transport replay."""
        direction = 23 if self.side == "BUY" else 24
        quantity = request.get("volume")
        known = state.data["requests"]
        if (
            request.get("stock_code") != self.code
            or request.get("direction") != direction
            or type(quantity) is not int or not 0 < quantity <= 100
            or (self.side == "BUY" and quantity != 100)
            or request.get("request_id") != self.request_id
            or request.get("order_remark") != self.request_id
            or set(known) != {self.request_id}
            or known[self.request_id]["request"] != request
        ):
            raise Stop("TRIAL_AUTHORIZATION_MISMATCH: no submission permitted")
