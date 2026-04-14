"""
WebSocket 持仓监控 — 秒级出场
- 买入成交后立即启动，订阅该币种实时价格流
- 每个价格tick检查止盈/止损/超时(秒)
- 触发出场后市价卖出，关闭WebSocket

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
    """

    def __init__(
        self,
        symbol: str,
        entry_price: float,
        qty: float,
        entry_time: str,
        # 出场参数（秒级）
        stop_loss_pct: float,       # 止损%，如 1.5
        take_profit_pct: float,     # 止盈%，如 2.5
        max_hold_seconds: int,      # 最大持仓秒数，如 5
        # 回调
        on_exit: Callable,          # on_exit(symbol, exit_price, reason, hold_seconds)
        # 可选
        mode: str = "paper",
    ):
        self.symbol          = symbol.lower()
        self.symbol_upper    = symbol.upper()
        self.entry_price     = entry_price
        self.qty             = qty
        self.entry_time      = entry_time
        self.stop_loss_pct   = stop_loss_pct
        self.take_profit_pct = take_profit_pct
        self.max_hold_seconds = max_hold_seconds
        self.on_exit         = on_exit
        self.mode            = mode

        self.entry_ts        = time.time()
        self.stop_loss_price = entry_price * (1 - stop_loss_pct / 100)
        self.peak_price      = entry_price
        self.exited          = False
        self._ws             = None
        self._thread: Optional[threading.Thread] = None

        # paper模式用REST轮询替代WebSocket
        self._poll_timer: Optional[threading.Timer] = None

    def start(self, rest_price_fn: Callable = None):
        """启动监控"""
        if self.exited:
            return
        if WS_AVAILABLE and self.mode == "live":
            self._start_websocket()
        else:
            # paper模式或WebSocket不可用：REST轮询
            self._rest_price_fn = rest_price_fn
            self._start_polling()

    def stop(self):
        """主动停止（外部调用）"""
        self.exited = True
        self._close_ws()
        if self._poll_timer:
            self._poll_timer.cancel()

    # ── WebSocket ────────────────────────────────────

    def _start_websocket(self):
        """订阅 bookTicker（最快的价格流，每次最优买卖价变化就推送）"""
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
        logger.info(f"[{self.symbol_upper}] WebSocket启动 止损={self.stop_loss_price:.6f} "
                    f"止盈={self.entry_price*(1+self.take_profit_pct/100):.6f} "
                    f"超时={self.max_hold_seconds}秒")

    def _on_message(self, ws, message):
        if self.exited:
            ws.close()
            return
        try:
            data = json.loads(message)
            # bookTicker: {"b":"买一价", "a":"卖一价"}
            # 用买一价作为当前可卖出价
            bid = float(data.get("b", 0))
            ask = float(data.get("a", 0))
            if bid <= 0:
                return
            # 用中间价作为参考
            mid = (bid + ask) / 2 if ask > 0 else bid
            self._check_exit(mid)
        except Exception as e:
            logger.error(f"[{self.symbol_upper}] WS消息处理异常: {e}")

    def _on_error(self, ws, error):
        logger.error(f"[{self.symbol_upper}] WebSocket错误: {error}")
        if not self.exited:
            # 降级到REST轮询
            self._start_polling()

    def _on_close(self, ws, close_status_code, close_msg):
        if not self.exited:
            logger.warning(f"[{self.symbol_upper}] WebSocket意外断开，降级REST轮询")
            self._start_polling()

    def _close_ws(self):
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
            self._ws = None

    # ── REST 轮询（降级方案） ─────────────────────────

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
            self._poll_timer = threading.Timer(1.0, self._poll_once)
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
            # 达到止盈后，止损上移到成本价保本
            gain = (self.peak_price - self.entry_price) / self.entry_price * 100
            if gain >= self.take_profit_pct:
                self.stop_loss_price = max(self.stop_loss_price, self.entry_price)

        exit_price  = None
        exit_reason = None

        # 1. 止损
        if current_price <= self.stop_loss_price:
            exit_price  = current_price
            exit_reason = f"止损{self.stop_loss_pct}%"

        # 2. 超时
        elif hold_seconds >= self.max_hold_seconds:
            exit_price  = current_price
            exit_reason = f"超时{self.max_hold_seconds}秒"

        # 3. 止盈
        elif (current_price - self.entry_price) / self.entry_price * 100 >= self.take_profit_pct:
            exit_price  = current_price
            exit_reason = f"止盈{self.take_profit_pct}%"

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


class PositionMonitorManager:
    """
    管理所有活跃持仓的WebSocket监控器。
    engine.py 通过此类启动/停止监控。
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
    ):
        """买入成交后调用，启动该币种的秒级监控"""
        symbol = symbol.upper()
        with self._lock:
            # 如果已有监控，先停止旧的
            if symbol in self._monitors:
                self._monitors[symbol].stop()

            monitor = PositionMonitor(
                symbol           = symbol,
                entry_price      = entry_price,
                qty              = qty,
                entry_time       = entry_time,
                stop_loss_pct    = cfg.get("ws_stop_loss_pct",    1.5),
                take_profit_pct  = cfg.get("ws_take_profit_pct",  2.5),
                max_hold_seconds = cfg.get("ws_max_hold_seconds", 5),
                on_exit          = on_exit,
                mode             = mode,
            )
            self._monitors[symbol] = monitor

        monitor.start(rest_price_fn=rest_price_fn)

    def stop_monitor(self, symbol: str):
        """主动停止某币种的监控（手动移除时调用）"""
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
        """出场完成后清理"""
        with self._lock:
            self._monitors.pop(symbol.upper(), None)
