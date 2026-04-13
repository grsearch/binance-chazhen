"""
插针策略核心逻辑
- 筛选币安24h涨幅>=50%的活跃币种
- 每根1分钟K线收盘时，若为下跌K线则以最低价为基准挂三档限价买单
- 下一根K线若无成交则撤单重挂
- 成交后跟踪止盈止损
"""

import time
import json
import threading
from dataclasses import dataclass, field, asdict
from typing import Optional
from datetime import datetime
from enum import Enum


class OrderStatus(Enum):
    PENDING   = "pending"
    FILLED    = "filled"
    CANCELLED = "cancelled"


class TradeStatus(Enum):
    OPEN   = "open"
    CLOSED = "closed"


@dataclass
class StrategyConfig:
    # 筛选条件
    min_gain_24h: float = 30.0       # 24h最低涨幅%
    min_volume_usdt: float = 500_000  # 最低成交额USDT

    # 挂单参数（基于当根K线最低价向下）
    order_pct_1: float = 4.0          # 第1档 -4%
    order_pct_2: float = 6.0          # 第2档 -6%
    order_pct_3: float = 9.0          # 第3档 -9%
    order_ratio_1: float = 0.40       # 第1档仓位比例
    order_ratio_2: float = 0.35       # 第2档仓位比例
    order_ratio_3: float = 0.25       # 第3档仓位比例

    # 触发挂单条件
    min_candle_drop_pct: float = 0.5  # K线最小跌幅%才挂单
    cooldown_candles: int = 2         # 撤单后冷却K线数

    # 卖出参数
    stop_loss_pct: float = 3.0        # 止损% (相对成交均价)
    take_profit_pct: float = 5.0      # 快速止盈%
    trailing_sell_on_red: bool = True  # 回弹后第一根阴线卖出
    max_hold_candles: int = 10        # 最大持仓K线数（强制出局）

    # 资金管理
    position_size_usdt: float = 100.0  # 每次总投入USDT


@dataclass
class Order:
    id: str
    symbol: str
    price: float
    qty: float
    ratio: float
    status: OrderStatus = OrderStatus.PENDING
    fill_price: Optional[float] = None
    fill_time: Optional[str] = None


@dataclass
class Trade:
    id: str
    symbol: str
    entry_price: float         # 加权平均成交价
    qty: float
    entry_time: str
    status: TradeStatus = TradeStatus.OPEN
    exit_price: Optional[float] = None
    exit_time: Optional[str] = None
    exit_reason: Optional[str] = None
    pnl_pct: float = 0.0
    pnl_usdt: float = 0.0
    hold_candles: int = 0


@dataclass
class SymbolState:
    """每个币种的状态机"""
    symbol: str
    active: bool = False           # 是否在监控中
    orders: list = field(default_factory=list)
    current_trade: Optional[Trade] = None
    cooldown_remaining: int = 0    # 冷却倒计时
    candles_since_entry: int = 0   # 持仓K线计数
    stop_loss_price: float = 0.0
    peak_price: float = 0.0        # 持仓期间最高价（跟踪止盈用）
    had_green_candle: bool = False  # 是否出现过回弹阳线


class SpikeStrategy:
    def __init__(self, config: StrategyConfig = None):
        self.config = config or StrategyConfig()
        self.states: dict[str, SymbolState] = {}
        self.all_trades: list[Trade] = []
        self.log_entries: list[dict] = []
        self._lock = threading.Lock()

    # ─────────────────────────────────────────
    # 筛选逻辑
    # ─────────────────────────────────────────
    def should_monitor(self, ticker: dict) -> bool:
        """判断是否应监控该币种"""
        gain = float(ticker.get("priceChangePercent", 0))
        volume = float(ticker.get("quoteVolume", 0))
        symbol = ticker.get("symbol", "")
        # 排除稳定币、杠杆代币
        blacklist = ["USDT", "BUSD", "USDC", "TUSD", "UPUSDT", "DOWNUSDT"]
        if any(b in symbol for b in blacklist):
            return False
        return gain >= self.config.min_gain_24h and volume >= self.config.min_volume_usdt

    # ─────────────────────────────────────────
    # 挂单逻辑
    # ─────────────────────────────────────────
    def should_place_orders(self, candle: dict) -> bool:
        """当根K线是否满足挂单条件"""
        open_p  = float(candle["open"])
        close_p = float(candle["close"])
        if close_p >= open_p:
            return False  # 阳线不挂
        drop_pct = (open_p - close_p) / open_p * 100
        if drop_pct < self.config.min_candle_drop_pct:
            return False  # 跌幅不够
        # 收盘价需低于上根收盘（连续下跌确认）
        prev_close = float(candle.get("prev_close", close_p + 1))
        return close_p < prev_close

    def calc_order_prices(self, low: float) -> list[tuple]:
        """以K线最低价为基准计算三档挂单价"""
        cfg = self.config
        return [
            (low * (1 - cfg.order_pct_1 / 100), cfg.order_ratio_1),
            (low * (1 - cfg.order_pct_2 / 100), cfg.order_ratio_2),
            (low * (1 - cfg.order_pct_3 / 100), cfg.order_ratio_3),
        ]

    def build_orders(self, symbol: str, low: float, ts: str) -> list[Order]:
        cfg = self.config
        total = cfg.position_size_usdt
        orders = []
        for i, (price, ratio) in enumerate(self.calc_order_prices(low)):
            qty = (total * ratio) / price
            orders.append(Order(
                id=f"{symbol}_{ts}_{i+1}",
                symbol=symbol,
                price=round(price, 6),
                qty=round(qty, 4),
                ratio=ratio,
            ))
        return orders

    # ─────────────────────────────────────────
    # 成交检测（虚拟盘用）
    # ─────────────────────────────────────────
    def check_fills(self, state: SymbolState, candle: dict) -> list[Order]:
        """检查哪些挂单在本K线内被插针触发"""
        low = float(candle["low"])
        filled = []
        for order in state.orders:
            if order.status == OrderStatus.PENDING and low <= order.price:
                order.status = OrderStatus.FILLED
                order.fill_price = order.price
                order.fill_time = candle.get("close_time", "")
                filled.append(order)
        return filled

    def open_trade(self, symbol: str, filled_orders: list[Order], ts: str) -> Trade:
        """用成交的订单开仓（加权均价）"""
        total_cost = sum(o.fill_price * o.qty for o in filled_orders)
        total_qty  = sum(o.qty for o in filled_orders)
        avg_price  = total_cost / total_qty
        return Trade(
            id=f"{symbol}_trade_{ts}",
            symbol=symbol,
            entry_price=round(avg_price, 6),
            qty=round(total_qty, 4),
            entry_time=ts,
        )

    # ─────────────────────────────────────────
    # 卖出逻辑
    # ─────────────────────────────────────────
    def check_exit(self, state: SymbolState, candle: dict) -> Optional[tuple[float, str]]:
        """
        返回 (exit_price, reason) 或 None
        优先级：止损 > 超时 > 快速止盈 > 阴线跟踪止盈
        """
        trade = state.current_trade
        if not trade:
            return None

        low   = float(candle["low"])
        high  = float(candle["high"])
        close = float(candle["close"])
        open_ = float(candle["open"])
        cfg   = self.config

        # 更新最高价
        if high > state.peak_price:
            state.peak_price = high

        # 1. 止损
        if low <= state.stop_loss_price:
            return (state.stop_loss_price, "止损")

        # 2. 超时强制出局
        state.candles_since_entry += 1
        if state.candles_since_entry >= cfg.max_hold_candles:
            return (close, "超时出局")

        # 3. 快速止盈
        gain_pct = (high - trade.entry_price) / trade.entry_price * 100
        if gain_pct >= cfg.take_profit_pct:
            # 止损上移到成本价
            state.stop_loss_price = trade.entry_price
            if close >= trade.entry_price * (1 + cfg.take_profit_pct / 100):
                return (close, f"止盈{cfg.take_profit_pct}%")

        # 4. 阴线跟踪止盈（需先出现阳线回弹）
        if close > trade.entry_price:
            state.had_green_candle = True
            # 止损上移到成本价（保本）
            if state.stop_loss_price < trade.entry_price:
                state.stop_loss_price = trade.entry_price

        if state.had_green_candle and cfg.trailing_sell_on_red:
            is_red = close < open_
            drop = (open_ - close) / open_ * 100
            if is_red and drop >= 1.0:
                return (close, "回弹后阴线")

        return None

    # ─────────────────────────────────────────
    # 主处理入口（每根K线收盘时调用）
    # ─────────────────────────────────────────
    def on_candle_close(self, symbol: str, candle: dict) -> dict:
        """
        处理一根已收盘K线，返回当前状态摘要
        candle: {open, high, low, close, volume, close_time, prev_close}
        """
        with self._lock:
            if symbol not in self.states:
                self.states[symbol] = SymbolState(symbol=symbol)
            state = self.states[symbol]
            ts    = candle.get("close_time", datetime.now().isoformat())
            events = []

            # ── 如果有持仓，先检查出场 ──
            if state.current_trade:
                result = self.check_exit(state, candle)
                if result:
                    exit_price, reason = result
                    trade = state.current_trade
                    trade.exit_price  = exit_price
                    trade.exit_time   = ts
                    trade.exit_reason = reason
                    trade.status      = TradeStatus.CLOSED
                    trade.pnl_pct     = (exit_price - trade.entry_price) / trade.entry_price * 100
                    trade.pnl_usdt    = trade.pnl_pct / 100 * trade.entry_price * trade.qty
                    trade.hold_candles = state.candles_since_entry
                    self.all_trades.append(trade)
                    events.append(f"出场: {reason} @ {exit_price:.6f} PnL={trade.pnl_pct:.2f}%")
                    # 重置状态
                    state.current_trade    = None
                    state.candles_since_entry = 0
                    state.had_green_candle = False
                    state.peak_price       = 0.0
                    state.stop_loss_price  = 0.0
                    state.cooldown_remaining = self.config.cooldown_candles
                    # 撤销剩余挂单
                    for o in state.orders:
                        if o.status == OrderStatus.PENDING:
                            o.status = OrderStatus.CANCELLED
                    state.orders = []

            # ── 冷却倒计时 ──
            if state.cooldown_remaining > 0:
                state.cooldown_remaining -= 1

            # ── 如果有未成交挂单，检查是否成交 ──
            if state.orders and not state.current_trade:
                filled = self.check_fills(state, candle)
                if filled:
                    trade = self.open_trade(symbol, filled, ts)
                    state.current_trade   = trade
                    state.stop_loss_price = trade.entry_price * (1 - self.config.stop_loss_pct / 100)
                    state.peak_price      = trade.entry_price
                    state.candles_since_entry = 0
                    events.append(f"入场: {len(filled)}档成交 均价={trade.entry_price:.6f}")

                # 撤销本轮剩余未成交挂单（下一根K线重新评估）
                cancelled = 0
                for o in state.orders:
                    if o.status == OrderStatus.PENDING:
                        o.status = OrderStatus.CANCELLED
                        cancelled += 1
                if cancelled:
                    events.append(f"撤单: {cancelled}档未成交")
                state.orders = [o for o in state.orders if o.status != OrderStatus.CANCELLED]

            # ── 评估是否挂新单 ──
            if (not state.current_trade
                    and state.cooldown_remaining == 0
                    and self.should_place_orders(candle)):
                low = float(candle["low"])
                new_orders = self.build_orders(symbol, low, ts)
                state.orders = new_orders
                prices = [f"{o.price:.4f}" for o in new_orders]
                events.append(f"挂单: {prices}")

            self._log(symbol, ts, candle, state, events)
            return self._summary(symbol, state, events)

    # ─────────────────────────────────────────
    # 统计
    # ─────────────────────────────────────────
    def get_stats(self) -> dict:
        trades = [t for t in self.all_trades if t.status == TradeStatus.CLOSED]
        if not trades:
            return {"total": 0}
        wins   = [t for t in trades if t.pnl_pct > 0]
        losses = [t for t in trades if t.pnl_pct <= 0]
        total_pnl = sum(t.pnl_usdt for t in trades)
        return {
            "total":       len(trades),
            "wins":        len(wins),
            "losses":      len(losses),
            "win_rate":    round(len(wins) / len(trades) * 100, 1),
            "total_pnl":   round(total_pnl, 2),
            "avg_win":     round(sum(t.pnl_pct for t in wins)   / len(wins),   2) if wins   else 0,
            "avg_loss":    round(sum(t.pnl_pct for t in losses) / len(losses), 2) if losses else 0,
            "max_win":     round(max((t.pnl_pct for t in trades), default=0), 2),
            "max_loss":    round(min((t.pnl_pct for t in trades), default=0), 2),
            "avg_hold":    round(sum(t.hold_candles for t in trades) / len(trades), 1),
        }

    def get_trades_json(self) -> list[dict]:
        return [asdict(t) for t in self.all_trades]

    def _log(self, symbol, ts, candle, state, events):
        if events:
            self.log_entries.append({
                "time": ts, "symbol": symbol,
                "events": events,
                "close": float(candle["close"]),
            })

    def _summary(self, symbol, state, events) -> dict:
        trade_info = None
        if state.current_trade:
            t = state.current_trade
            trade_info = {
                "entry_price": t.entry_price,
                "qty": t.qty,
                "entry_time": t.entry_time,
                "stop_loss": state.stop_loss_price,
                "hold_candles": state.candles_since_entry,
            }
        return {
            "symbol":       symbol,
            "has_orders":   len([o for o in state.orders if o.status == OrderStatus.PENDING]),
            "in_trade":     state.current_trade is not None,
            "trade":        trade_info,
            "cooldown":     state.cooldown_remaining,
            "events":       events,
        }
