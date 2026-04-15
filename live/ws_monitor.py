"""
WebSocket 持仓监控 — 秒级出场 v3.0

出场策略：
  - 第3秒：如果有利润（当前价 > 买入均价），立即市价清仓
  - 第3秒：如果没利润，继续持有
  - 第6秒：不论盈亏，强制市价清仓
  - 中途止损：如果跌破止损线，立即清仓（不等3秒）

新增 UserDataStream：
  - 买入挂单后立即订阅用户数据流
  - 订单成交瞬间推送，延迟从60秒降到毫秒级
  - 成交后自动启动价格监控

依赖：websocket-client
  pip install websocket-client
"""

import json
import time
import threading
import logging
from typing import Callable, Optional

logger = logging.getLogger(__name__)

try:
    import websocket
    WS_AVAILABLE = True
except ImportError:
    WS_AVAILABLE = False
    logger.warning("websocket-client 未安装，将回退到REST轮询出场")


class PositionMonitor:
    """
    单个持仓的秒级WebSocket监控器。
    成交后立即创建，出场后自动销毁。

    出场逻辑：
      1. 随时止损（跌破止损线立即卖）
      2. 第3秒检查：有利润→卖，无利润→继续
      3. 第6秒强制卖出（不论盈亏）
    """

    def __init__(
        self,
        symbol: str,
        entry_price: float,
        qty: float,
        entry_time: str,
        # 出场参数
        stop_loss_pct: float,           # 止损%，如 1.5
        first_check_seconds: int,       # 第一次检查秒数（默认3）
        force_exit_seconds: int,        # 强制出场秒数（默认6）
        # 回调
        on_exit: Callable,
        # 可选
        mode: str = "paper",
        real_entry_ts: float = 0,       # 币安实际成交时间戳（秒）
    ):
        self.symbol          = symbol.lower()
        self.symbol_upper    = symbol.upper()
        self.entry_price     = entry_price
        self.qty             = qty
        self.entry_time      = entry_time
        self.stop_loss_pct   = stop_loss_pct
        self.first_check_seconds  = first_check_seconds
        self.force_exit_seconds   = force_exit_seconds
        self.on_exit         = on_exit
        self.mode            = mode

        # 用币安实际成交时间（如果有），否则用当前时间
        self.entry_ts        = real_entry_ts if real_entry_ts > 0 else time.time()
        self.stop_loss_price = entry_price * (1 - stop_loss_pct / 100)
        self.peak_price      = entry_price
        self.exited          = False
        self._ws             = None
        self._thread: Optional[threading.Thread] = None
        self._poll_timer: Optional[threading.Timer] = None
        self._rest_price_fn  = None

        # 第3秒检查标记
        self._first_checked  = False

    def start(self, rest_price_fn: Callable = None):
        """启动监控"""
        if self.exited:
            return
        self._rest_price_fn = rest_price_fn
        if WS_AVAILABLE and self.mode == "live":
            self._start_websocket()
        else:
            self._start_polling()

    def stop(self):
        """主动停止"""
        self.exited = True
        self._close_ws()
        if self._poll_timer:
            self._poll_timer.cancel()

    # ── WebSocket ────────────────────────────────────

    def _start_websocket(self):
        """订阅 bookTicker（最快的价格流）"""
        url = f"wss://stream.binance.com:9443/ws/{self.symbol}@bookTicker"
        self._ws = websocket.WebSocketApp(
            url,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        self._thread = threading.Thread(
            target=self._ws.run_forever,
            kwargs={"ping_interval": 20, "ping_timeout": 10},
            daemon=True,
        )
        self._thread.start()
        logger.info(
            f"[{self.symbol_upper}] WS监控启动 "
            f"入场价={self.entry_price:.6f} "
            f"止损={self.stop_loss_price:.6f} "
            f"第{self.first_check_seconds}秒盈利检查 "
            f"第{self.force_exit_seconds}秒强制出场"
        )

    def _on_message(self, ws, message):
        if self.exited:
            ws.close()
            return
        try:
            data = json.loads(message)
            bid = float(data.get("b", 0))
            ask = float(data.get("a", 0))
            if bid <= 0:
                return
            mid = (bid + ask) / 2 if ask > 0 else bid
            self._check_exit(mid)
        except Exception as e:
            logger.error(f"[{self.symbol_upper}] WS消息处理异常: {e}")

    def _on_error(self, ws, error):
        logger.error(f"[{self.symbol_upper}] WebSocket错误: {error}")
        if not self.exited:
            self._start_polling()

    def _on_close(self, ws, close_status_code, close_msg):
        if not self.exited:
            logger.warning(f"[{self.symbol_upper}] WebSocket断开，降级REST轮询")
            self._start_polling()

    def _close_ws(self):
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
            self._ws = None

    # ── REST 轮询（降级 / paper模式） ─────────────────

    def _start_polling(self):
        if self.exited:
            return
        self._poll_once()

    def _poll_once(self):
        if self.exited:
            return
        try:
            if self._rest_price_fn:
                price = self._rest_price_fn(self.symbol_upper)
                if price and price > 0:
                    self._check_exit(price)
        except Exception as e:
            logger.error(f"[{self.symbol_upper}] REST轮询异常: {e}")
        if not self.exited:
            self._poll_timer = threading.Timer(0.5, self._poll_once)
            self._poll_timer.daemon = True
            self._poll_timer.start()

    # ── 出场判断（每个价格tick调用） ─────────────────

    def _check_exit(self, current_price: float):
        if self.exited:
            return

        hold_seconds = time.time() - self.entry_ts

        # 更新最高价
        if current_price > self.peak_price:
            self.peak_price = current_price

        exit_price  = None
        exit_reason = None

        # ── 1. 止损（任何时刻，立即执行） ──
        if current_price <= self.stop_loss_price:
            exit_price  = current_price
            exit_reason = f"止损{self.stop_loss_pct}%"

        # ── 2. 第一次检查点：有利润就卖 ──
        elif hold_seconds >= self.first_check_seconds and not self._first_checked:
            self._first_checked = True
            pnl = (current_price - self.entry_price) / self.entry_price * 100
            if current_price > self.entry_price:
                exit_price  = current_price
                exit_reason = f"第{self.first_check_seconds}秒止盈({pnl:+.2f}%)"
            else:
                logger.info(
                    f"[{self.symbol_upper}] 第{self.first_check_seconds}秒无利润"
                    f"({pnl:+.2f}%)，延迟到第{self.force_exit_seconds}秒"
                )

        # ── 3. 强制出场 ──
        elif hold_seconds >= self.force_exit_seconds:
            pnl = (current_price - self.entry_price) / self.entry_price * 100
            exit_price  = current_price
            exit_reason = f"第{self.force_exit_seconds}秒强制出场({pnl:+.2f}%)"

        if exit_price is not None:
            self.exited = True
            self._close_ws()
            if self._poll_timer:
                self._poll_timer.cancel()
            try:
                self.on_exit(
                    symbol       = self.symbol_upper,
                    exit_price   = exit_price,
                    reason       = exit_reason,
                    hold_seconds = round(hold_seconds, 1),
                    entry_price  = self.entry_price,
                    qty          = self.qty,
                    entry_time   = self.entry_time,
                )
            except Exception as e:
                logger.error(f"[{self.symbol_upper}] on_exit回调异常: {e}")


class UserDataStreamMonitor:
    """
    用户数据流监控器。
    订阅币安 userDataStream，实时接收订单成交推送。
    解决主循环60秒轮询导致的成交感知延迟问题。

    工作流：
      1. engine 启动时创建并 start()
      2. 挂买单后调用 add_pending(symbol)
      3. 币安推送成交 → on_fill 回调 → engine 立即开仓 + 启动价格监控
    """

    def __init__(self, client, on_fill: Callable, mode: str = "paper"):
        self._client   = client
        self._on_fill  = on_fill
        self._mode     = mode
        self._ws       = None
        self._thread: Optional[threading.Thread] = None
        self._listen_key = None
        self._keepalive_timer: Optional[threading.Timer] = None
        self._stopped  = False

        # 哪些 symbol 有挂单等待成交
        self._pending_symbols: set[str] = set()
        self._lock = threading.Lock()

        # 累计成交（多档合并用）
        self._fill_accumulator: dict = {}
        # debounce 定时器
        self._debounce_timers: dict = {}

    def start(self):
        """启动用户数据流"""
        if not self._client or self._mode != "live":
            logger.info("UserDataStream: 非live模式或无client，跳过")
            return
        if not WS_AVAILABLE:
            logger.warning("UserDataStream: websocket-client未安装，跳过")
            return
        try:
            self._listen_key = self._create_listen_key()
            if not self._listen_key:
                logger.error("UserDataStream: 获取listenKey失败")
                return
            self._start_ws()
            self._start_keepalive()
            logger.info("UserDataStream: 已启动")
        except Exception as e:
            logger.error(f"UserDataStream: 启动失败: {e}")

    def stop(self):
        """停止"""
        self._stopped = True
        if self._keepalive_timer:
            self._keepalive_timer.cancel()
        for t in self._debounce_timers.values():
            t.cancel()
        self._debounce_timers.clear()
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
        if self._listen_key and self._client:
            try:
                import urllib.request
                url = f"https://api.binance.com/api/v3/userDataStream?listenKey={self._listen_key}"
                req = urllib.request.Request(url, method="DELETE", headers={
                    "X-MBX-APIKEY": self._client.api_key,
                })
                urllib.request.urlopen(req, timeout=5)
            except Exception:
                pass
        logger.info("UserDataStream: 已停止")

    def add_pending(self, symbol: str):
        """注册等待成交的 symbol"""
        with self._lock:
            self._pending_symbols.add(symbol.upper())

    def remove_pending(self, symbol: str):
        """撤单后移除"""
        with self._lock:
            self._pending_symbols.discard(symbol.upper())
            self._fill_accumulator.pop(symbol.upper(), None)

    def _create_listen_key(self) -> Optional[str]:
        import urllib.request as _req
        import json as _json
        url = "https://api.binance.com/api/v3/userDataStream"
        req = _req.Request(url, method="POST", headers={
            "X-MBX-APIKEY": self._client.api_key,
        }, data=b"")
        with _req.urlopen(req, timeout=10) as r:
            data = _json.loads(r.read())
        return data.get("listenKey")

    def _keepalive_listenkey(self):
        if self._stopped:
            return
        try:
            import urllib.request as _req
            url = f"https://api.binance.com/api/v3/userDataStream?listenKey={self._listen_key}"
            req = _req.Request(url, method="PUT", headers={
                "X-MBX-APIKEY": self._client.api_key,
            }, data=b"")
            _req.urlopen(req, timeout=10)
        except Exception as e:
            logger.error(f"UserDataStream: listenKey续期失败: {e}")
        self._start_keepalive()

    def _start_keepalive(self):
        if self._stopped:
            return
        self._keepalive_timer = threading.Timer(25 * 60, self._keepalive_listenkey)
        self._keepalive_timer.daemon = True
        self._keepalive_timer.start()

    def _start_ws(self):
        url = f"wss://stream.binance.com:9443/ws/{self._listen_key}"
        self._ws = websocket.WebSocketApp(
            url,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        self._thread = threading.Thread(
            target=self._ws.run_forever,
            kwargs={"ping_interval": 20, "ping_timeout": 10},
            daemon=True,
        )
        self._thread.start()

    def _on_message(self, ws, message):
        if self._stopped:
            return
        try:
            data = json.loads(message)
            if data.get("e") == "executionReport":
                self._handle_execution_report(data)
        except Exception as e:
            logger.error(f"UserDataStream: 消息处理异常: {e}")

    def _handle_execution_report(self, data: dict):
        """
        处理订单执行报告。
          s=symbol, S=side, X=orderStatus, x=executionType
          L=本次成交价, l=本次成交量, z=累计量, Z=累计额
          T=成交时间(ms), c=clientOrderId
        """
        symbol      = data.get("s", "")
        side        = data.get("S", "")
        exec_type   = data.get("x", "")
        last_price  = float(data.get("L", 0))
        last_qty    = float(data.get("l", 0))
        cum_qty     = float(data.get("z", 0))
        cum_quote   = float(data.get("Z", 0))
        transact_ts = int(data.get("T", 0))

        # 只关心 BUY 订单的实际成交
        if side != "BUY" or exec_type != "TRADE":
            return

        with self._lock:
            if symbol not in self._pending_symbols:
                return

        logger.info(
            f"UserDataStream: {symbol} BUY成交 "
            f"价={last_price} 量={last_qty} "
            f"累计量={cum_qty} 累计额={cum_quote}"
        )

        # 累积成交（多档可能分开推送）
        with self._lock:
            if symbol not in self._fill_accumulator:
                self._fill_accumulator[symbol] = {
                    "fills": [],
                    "transact_ts": transact_ts,
                }
            acc = self._fill_accumulator[symbol]
            acc["fills"].append({"price": last_price, "qty": last_qty})
            if transact_ts > acc["transact_ts"]:
                acc["transact_ts"] = transact_ts

        # debounce 0.5秒（等待同批次多档成交合并）
        existing = self._debounce_timers.get(symbol)
        if existing:
            existing.cancel()
        t = threading.Timer(0.5, self._process_fills, args=(symbol,))
        t.daemon = True
        t.start()
        self._debounce_timers[symbol] = t

    def _process_fills(self, symbol: str):
        """合并成交后回调"""
        with self._lock:
            acc = self._fill_accumulator.pop(symbol, None)
            self._pending_symbols.discard(symbol)
            self._debounce_timers.pop(symbol, None)

        if not acc or not acc["fills"]:
            return

        fills = acc["fills"]
        total_cost = sum(f["price"] * f["qty"] for f in fills)
        total_qty  = sum(f["qty"] for f in fills)
        avg_price  = total_cost / total_qty if total_qty > 0 else 0

        logger.info(
            f"UserDataStream: {symbol} 合并{len(fills)}笔成交 "
            f"均价={avg_price:.6f} 总量={total_qty:.6f}"
        )

        try:
            self._on_fill(
                symbol           = symbol,
                avg_price        = avg_price,
                total_qty        = total_qty,
                transact_time_ms = acc["transact_ts"],
            )
        except Exception as e:
            logger.error(f"UserDataStream: on_fill回调异常: {e}")

    def _on_error(self, ws, error):
        logger.error(f"UserDataStream: WebSocket错误: {error}")
        if not self._stopped:
            threading.Timer(3.0, self._reconnect).start()

    def _on_close(self, ws, close_status_code, close_msg):
        if not self._stopped:
            logger.warning("UserDataStream: 连接断开，3秒后重连")
            threading.Timer(3.0, self._reconnect).start()

    def _reconnect(self):
        if self._stopped:
            return
        try:
            self._listen_key = self._create_listen_key()
            if self._listen_key:
                self._start_ws()
                logger.info("UserDataStream: 重连成功")
        except Exception as e:
            logger.error(f"UserDataStream: 重连失败: {e}")


class PositionMonitorManager:
    """
    管理所有活跃持仓的WebSocket监控器。
    """

    def __init__(self):
        self._monitors: dict[str, PositionMonitor] = {}
        self._lock = threading.Lock()

    def start_monitor(
        self,
        symbol: str,
        entry_price: float,
        qty: float,
        entry_time: str,
        cfg: dict,
        on_exit: Callable,
        mode: str,
        rest_price_fn: Callable = None,
        real_entry_ts: float = 0,
    ):
        """买入成交后调用，启动秒级监控"""
        symbol = symbol.upper()
        with self._lock:
            if symbol in self._monitors:
                self._monitors[symbol].stop()

            monitor = PositionMonitor(
                symbol              = symbol,
                entry_price         = entry_price,
                qty                 = qty,
                entry_time          = entry_time,
                stop_loss_pct       = cfg.get("ws_stop_loss_pct", 1.5),
                first_check_seconds = cfg.get("ws_first_check_seconds", 3),
                force_exit_seconds  = cfg.get("ws_force_exit_seconds", 6),
                on_exit             = on_exit,
                mode                = mode,
                real_entry_ts       = real_entry_ts,
            )
            self._monitors[symbol] = monitor

        monitor.start(rest_price_fn=rest_price_fn)

    def stop_monitor(self, symbol: str):
        symbol = symbol.upper()
        with self._lock:
            m = self._monitors.pop(symbol, None)
        if m:
            m.stop()

    def stop_all(self):
        with self._lock:
            monitors = list(self._monitors.values())
            self._monitors.clear()
        for m in monitors:
            m.stop()

    def active_symbols(self) -> list[str]:
        with self._lock:
            return list(self._monitors.keys())

    def on_exit_done(self, symbol: str):
        with self._lock:
            self._monitors.pop(symbol.upper(), None)
