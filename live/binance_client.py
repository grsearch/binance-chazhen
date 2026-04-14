"""
币安现货 REST 客户端
- 公开接口：行情、K线、涨幅榜
- 签名接口：下单、撤单、查询、账户余额
"""

import hmac
import hashlib
import time
import urllib.request
import urllib.parse
import urllib.error
import json
import logging
from decimal import Decimal, ROUND_DOWN
from typing import Optional


class BinanceAPIError(Exception):
    """币安API错误，包含HTTP状态码和币安错误码"""
    def __init__(self, http_code: int, binance_code: int, msg: str):
        self.http_code     = http_code
        self.binance_code  = binance_code
        self.msg           = msg
        super().__init__(f"HTTP {http_code} | 币安错误 {binance_code}: {msg}")

logger = logging.getLogger(__name__)

BASE = "https://api.binance.com"


class BinanceClient:
    def __init__(self, api_key: str, api_secret: str):
        self.api_key    = api_key
        self.api_secret = api_secret

    # ── 内部工具 ──────────────────────────────────────

    def _sign(self, params: dict) -> str:
        query = urllib.parse.urlencode(params)
        return hmac.new(
            self.api_secret.encode(), query.encode(), hashlib.sha256
        ).hexdigest()

    def _get(self, path: str, params: dict = None, signed: bool = False) -> dict:
        params = params or {}
        if signed:
            params["timestamp"] = int(time.time() * 1000)
            params["signature"] = self._sign(params)
        url = BASE + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={
            "X-MBX-APIKEY": self.api_key,
            "User-Agent": "spike-bot/2.0",
        })
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        return data

    def _post(self, path: str, params: dict) -> dict:
        params["timestamp"] = int(time.time() * 1000)
        params["signature"] = self._sign(params)
        body = urllib.parse.urlencode(params).encode()
        req  = urllib.request.Request(
            BASE + path, data=body, method="POST",
            headers={
                "X-MBX-APIKEY": self.api_key,
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent":   "spike-bot/2.0",
            }
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            try:
                err = json.loads(body)
                # 抛出带币安错误码和消息的异常，方便日志定位
                raise BinanceAPIError(e.code, err.get("code", 0), err.get("msg", body))
            except (json.JSONDecodeError, KeyError):
                raise BinanceAPIError(e.code, 0, body)

    def _delete(self, path: str, params: dict) -> dict:
        params["timestamp"] = int(time.time() * 1000)
        params["signature"] = self._sign(params)
        url  = BASE + path + "?" + urllib.parse.urlencode(params)
        req  = urllib.request.Request(url, method="DELETE", headers={
            "X-MBX-APIKEY": self.api_key,
            "User-Agent":   "spike-bot/2.0",
        })
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            try:
                err = json.loads(body)
                raise BinanceAPIError(e.code, err.get("code", 0), err.get("msg", body))
            except (json.JSONDecodeError, KeyError):
                raise BinanceAPIError(e.code, 0, body)

    # ── 公开接口 ──────────────────────────────────────

    def get_ticker_24h(self, symbol: str = None) -> list | dict:
        """获取24h行情（不传symbol则返回全部）"""
        params = {}
        if symbol:
            params["symbol"] = symbol
        return self._get("/api/v3/ticker/24hr", params)

    def get_klines(self, symbol: str, interval: str = "1m",
                   limit: int = 3, start_ms: int = None, end_ms: int = None) -> list:
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        if start_ms:
            params["startTime"] = start_ms
        if end_ms:
            params["endTime"] = end_ms
        return self._get("/api/v3/klines", params)

    def get_exchange_info(self, symbol: str) -> dict:
        """获取交易对精度信息"""
        return self._get("/api/v3/exchangeInfo", {"symbol": symbol})

    def get_symbol_filters(self, symbol: str) -> dict:
        """返回 {tick_size, step_size, min_qty, min_notional}"""
        info    = self.get_exchange_info(symbol)
        filters = {f["filterType"]: f for f in info["symbols"][0]["filters"]}
        return {
            "tick_size":    float(filters["PRICE_FILTER"]["tickSize"]),
            "step_size":    float(filters["LOT_SIZE"]["stepSize"]),
            "min_qty":      float(filters["LOT_SIZE"]["minQty"]),
            "min_notional": float(filters.get("NOTIONAL", {}).get("minNotional", 5)),
        }

    # ── 签名接口 ──────────────────────────────────────

    def get_account(self) -> dict:
        """获取账户余额"""
        return self._get("/api/v3/account", signed=True)

    def get_usdt_balance(self) -> float:
        """获取USDT可用余额"""
        account = self.get_account()
        for b in account["balances"]:
            if b["asset"] == "USDT":
                return float(b["free"])
        return 0.0

    def get_asset_balance(self, asset: str) -> float:
        """获取指定资产可用余额"""
        account = self.get_account()
        for b in account["balances"]:
            if b["asset"] == asset:
                return float(b["free"])
        return 0.0

    def place_limit_buy(self, symbol: str, price: float,
                        qty: float, client_order_id: str = None) -> dict:
        """挂限价买单"""
        params = {
            "symbol":    symbol,
            "side":      "BUY",
            "type":      "LIMIT",
            "timeInForce": "GTC",
            "price":     self._fmt_price(price, symbol),
            "quantity":  self._fmt_qty(qty, symbol),
        }
        if client_order_id:
            params["newClientOrderId"] = client_order_id
        logger.info(f"[下单] BUY {symbol} qty={params['quantity']} price={params['price']}")
        return self._post("/api/v3/order", params)

    def place_market_sell(self, symbol: str, qty: float) -> dict:
        """市价卖出（止损/超时出局用）"""
        params = {
            "symbol":   symbol,
            "side":     "SELL",
            "type":     "MARKET",
            "quantity": self._fmt_qty(qty, symbol),
        }
        logger.info(f"[市价卖] SELL {symbol} qty={params['quantity']}")
        return self._post("/api/v3/order", params)

    def place_limit_sell(self, symbol: str, price: float, qty: float,
                         client_order_id: str = None) -> dict:
        """挂限价卖单（止盈用）"""
        params = {
            "symbol":      symbol,
            "side":        "SELL",
            "type":        "LIMIT",
            "timeInForce": "GTC",
            "price":       self._fmt_price(price, symbol),
            "quantity":    self._fmt_qty(qty, symbol),
        }
        if client_order_id:
            params["newClientOrderId"] = client_order_id
        logger.info(f"[限价卖] SELL {symbol} qty={params['quantity']} price={params['price']}")
        return self._post("/api/v3/order", params)

    def cancel_order(self, symbol: str, order_id: int) -> dict:
        """撤单"""
        logger.info(f"[撤单] {symbol} orderId={order_id}")
        return self._delete("/api/v3/order", {
            "symbol": symbol, "orderId": order_id
        })

    def cancel_all_orders(self, symbol: str) -> list:
        """撤销某币种全部挂单"""
        logger.info(f"[撤全部] {symbol}")
        return self._delete("/api/v3/openOrders", {"symbol": symbol})

    def get_open_orders(self, symbol: str = None) -> list:
        """查询当前挂单"""
        params = {}
        if symbol:
            params["symbol"] = symbol
        return self._get("/api/v3/openOrders", params, signed=True)

    def get_order(self, symbol: str, order_id: int) -> dict:
        """查询单个订单状态"""
        return self._get("/api/v3/order", {
            "symbol": symbol, "orderId": order_id
        }, signed=True)

    # ── 精度处理 ──────────────────────────────────────

    # 缓存避免重复请求
    _filters_cache: dict = {}

    def _get_filters(self, symbol: str) -> dict:
        if symbol not in self._filters_cache:
            self._filters_cache[symbol] = self.get_symbol_filters(symbol)
        return self._filters_cache[symbol]

    def _fmt_price(self, price: float, symbol: str) -> str:
        """按交易对tick_size格式化价格（用Decimal避免浮点误差）"""
        f    = self._get_filters(symbol)
        tick = f["tick_size"]
        tick_d  = Decimal(str(tick))
        price_d = Decimal(str(price))
        # 向下取整到tick精度（保守，不会超过实际价格）
        formatted = (price_d / tick_d).to_integral_value(rounding=ROUND_DOWN) * tick_d
        # 格式化小数位数与tick一致
        return str(formatted.normalize()) if '.' in str(tick_d.normalize()) else str(int(formatted))

    def _fmt_qty(self, qty: float, symbol: str) -> str:
        """按交易对step_size格式化数量（Decimal精确截断 + 最小数量检查）"""
        f    = self._get_filters(symbol)
        step = f["step_size"]
        min_qty = f["min_qty"]
        step_d = Decimal(str(step))
        qty_d  = Decimal(str(qty))
        floored = (qty_d / step_d).to_integral_value(rounding=ROUND_DOWN) * step_d
        if float(floored) < min_qty:
            raise ValueError(
                f"数量 {float(floored)} 低于最小下单量 {min_qty}（{symbol}），"
                f"请增加 position_size_usdt"
            )
        # 格式化：与step小数位数一致，不产生科学计数法
        step_str = str(step_d.normalize())
        if '.' in step_str:
            decimals = len(step_str.split('.')[-1])
            return f"{float(floored):.{decimals}f}"
        return str(int(floored))
