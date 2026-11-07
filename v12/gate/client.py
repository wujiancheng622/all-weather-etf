"""Adapted from R5 live_exec/gate_orders.py (2026-11-07). V12 simulation only."""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.parse
import urllib.request

# 网关 409 拒单原因（原文子串）→ 执行器动作。未列出的一律 STOP：看不懂就停，不猜。
SKIP, REPRICE, OWN_OR_STOP, STOP = "SKIP", "REPRICE", "OWN_OR_STOP", "STOP"
RETRY_LATER, RETRY_NOW_NEW_ID, SKIP_OWNED = "RETRY_LATER", "RETRY_NOW_NEW_ID", "SKIP_OWNED"
_REFUSALS = (
    ("strategy or global broker cash insufficient", SKIP),
    ("broker position/sellable evidence is not ready for sell", SKIP),
    ("broker instrument is suspended from trading", RETRY_LATER),
    ("broker instrument lacks the supported continuous cash-stock evidence", RETRY_LATER),
    ("gateway busy with other orders; not sent, retry with a new request_id", RETRY_NOW_NEW_ID),
    ("instrument is unowned or belongs to another strategy", SKIP_OWNED),
    ("fixed execution price outside verified exchange band", REPRICE),
    ("another unsettled intent owns this security", OWN_OR_STOP),
)


class Transport(Exception):
    """不知道请求是否到达网关（超时、断连、5xx）。下单只能用同一请求重放，绝不换号。"""


class Refused(Exception):
    """网关明确拒绝（4xx）。action 见 classify()。"""

    def __init__(self, status, reason, action):
        # Persist known contract reasons only; arbitrary server bodies are not logs.
        safe = next((text for text, _ in _REFUSALS if text in reason), "UNCLASSIFIED_GATEWAY_REFUSAL")
        super().__init__(f"{status}: {safe}")
        self.status, self.reason, self.action = status, safe, action


def classify(status, reason):
    """把网关拒绝映射到 SKIP（这一只放弃）/REPRICE（按涨跌停重算一次）/OWN_OR_STOP/STOP。"""
    if status == 409:
        for text, action in _REFUSALS:
            if text in reason:
                return action
    return STOP


def _reason(payload):
    if isinstance(payload, dict):
        detail = payload.get("detail", payload)
        if isinstance(detail, dict):
            return str(detail.get("reason") or detail)
        return str(detail)
    return str(payload)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


class GateClient:
    """一个策略的网关客户端；令牌只发给策略接口。opener 可注入（测试用假网关）。"""

    def __init__(self, base_url, strategy_id, token, *, timeout=15, opener=None, write=False):
        if base_url != "http://127.0.0.1:8001" or strategy_id != "v12":
            raise ValueError("Expected the local simulation gateway and v12 strategy")
        if not isinstance(token, str) or len(token) < 24:
            raise ValueError("Strategy identity and its token required")
        self.base, self.strategy_id, self._token = base_url.rstrip("/"), strategy_id, token
        self.timeout = timeout
        self.write = write
        self._open = (
            opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect()).open
        )

    def _call(self, method, path, body=None, *, scoped=True, prefix="/api/v1"):
        if method != "GET" and not self.write:
            raise RuntimeError("Read-only gateway client")
        url = self.base + prefix + path
        headers = {"Content-Type": "application/json"}
        if scoped:
            headers["x-strategy-token"] = self._token
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self._open(request, timeout=self.timeout) as response:
                status, text = response.status, response.read().decode()
        except urllib.error.HTTPError as exc:
            status, text = exc.code, exc.read().decode(errors="replace")
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as exc:
            raise Transport(f"{method} {path}: {type(exc).__name__}") from None
        try:
            payload = json.loads(text) if text else None
        except json.JSONDecodeError:
            payload = text
        if status >= 500:
            raise Transport(f"{method} {path}: HTTP {status}")
        if not 200 <= status < 300:
            reason = _reason(payload)
            raise Refused(status, reason, classify(status, reason))
        return payload

    # ---- 只读 ----
    def health(self):
        return self._call("GET", "/health", scoped=False, prefix="")

    def view(self):
        return self._call("GET", f"/strategy-accounts/{self.strategy_id}")

    def intents(self):
        return self._call("GET", f"/strategy-accounts/{self.strategy_id}/intents")["intents"]

    def orders(self):
        return self._call("GET", f"/strategy-accounts/{self.strategy_id}/orders")["orders"]

    def trades(self):
        return self._call("GET", f"/strategy-accounts/{self.strategy_id}/trades")["trades"]

    def events(self, day):
        return self._call(
            "GET",
            f"/strategy-accounts/{self.strategy_id}/events?" + urllib.parse.urlencode({"day": day}),
        )

    def order(self, intent_id):
        return self._call("GET", f"/strategy-accounts/{self.strategy_id}/orders/{intent_id}")

    def positions(self):
        return self._call("GET", "/positions", scoped=False)["positions"]

    def tick(self, code):
        return self._call("GET", "/tick?" + urllib.parse.urlencode({"stock_code": code}), scoped=False)

    def instrument(self, code):
        return self._call(
            "GET", "/instrument?" + urllib.parse.urlencode({"stock_code": code}), scoped=False
        )

    # ---- 写（不自动重试）----
    def submit(self, request):
        """request 含 request_id/stock_code/volume/direction/limit_price/fee_ceiling/order_remark/send_price。

        202 返回 {intent_id, status, ...}；本地明确拒绝的重放也是 202 但 status=REJECTED、带 reason。
        """
        payload = self._call("POST", f"/strategy-accounts/{self.strategy_id}/orders", request)
        if isinstance(payload, dict) and payload.get("status") == "REJECTED":
            reason = str(payload.get("reason") or "")
            raise Refused(409, reason, classify(409, reason))
        return payload

    def refresh(self, intent_id):
        return self._call("POST", f"/strategy-accounts/{self.strategy_id}/orders/{intent_id}/refresh")

    def cancel(self, intent_id):
        return self._call("POST", f"/strategy-accounts/{self.strategy_id}/orders/{intent_id}/cancel")


def is_terminal(intent):
    """网关意图到确定结果：DONE 且（无成交或费用已按规则最终确认）。"""
    return intent.get("state") == "DONE" and (
        intent.get("filled") == 0 or intent.get("fees_confirmed") is True
    )
