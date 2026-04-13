"""
插针策略核心逻辑 v1.1
- 筛选币安24h涨幅>=50%的活跃币种（现货USDT对）
- 每根1分钟K线收盘时，若为下跌K线则以最低价为基准挂三档限价买单
- 下一根K线结束时撤单重新评估（仅在价格继续下跌时重新挂）
- 成交后跟踪止盈止损

Bug修复：
  [1] should_monitor: 黑名单用精确symbol匹配 + base关键字过滤，避免"USDT"误杀所有对
  [2] check_exit: candles_since_entry统一在顶部递增，止损分支不再漏计
  [3] pnl_usdt: 改为 (exit-entry)*qty，不再用百分比反推（避免浮点误差）
  [4] on_candle_close: 有持仓时直接返回，不再走挂单评估分支
  [5] build_orders: 价格精度改为8位，兼容低价小币
  [6] get_trades_json: 枚举值序列化为字符串
"""

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
    # 筛选
    min_gain_24h: float    = 50.0
    min_volume_usdt: float = 500_000.0

    # 精确黑名单（稳定币交易对）
    symbol_blacklist: tuple = (
        "USDCUSDT", "BUSDUSDT", "TUSDUSDT", "FDUSDUSDT", "DAIUSDT",
    )
    # 杠杆代币关键字（匹配BASE部分）
    name_blacklist: tuple = ("UP", "DOWN", "BULL", "BEAR", "3L", "3S")

    # 挂单参数
    order_pct_1: float   = 4.0
    order_pct_2: float   = 6.0
    order_pct_3: float   = 9.0
    order_ratio_1: float = 0.40
    order_ratio_2: float = 0.35
    order_ratio_3: float = 0.25

    # 触发挂单条件
    min_candle_drop_pct: float = 0.5
    cooldown_candles: int      = 2

    # 卖出参数
    stop_loss_pct: float       = 3.0
    take_profit_pct: float     = 5.0
    trailing_sell_on_red: bool = True
    trailing_red_min_pct: float = 1.0   # 阴线触发跟踪止盈的最小跌幅
    max_hold_candles: int      = 10

    # 资金
    position_size_usdt: float = 100.0


@dataclass
class Order:
    id: str
    symbol: str
    price: float
    qty: float
    ratio: float
    status: OrderStatus          = OrderStatus.PENDING
    fill_price: Optional[float]  = None
    fill_time: Optional[str]     = None


@dataclass
class Trade:
    id: str
    symbol: str
    entry_price: float
    qty: float
    entry_time: str
    status: TradeStatus          = TradeStatus.OPEN
    exit_price: Optional[float]  = None
    exit_time: Optional[str]     = None
    exit_reason: Optional[str]   = None
    pnl_pct: float               = 0.0
    pnl_usdt: float              = 0.0
    hold_candles: int            = 0


@dataclass
class SymbolState:
    symbol: str
    orders: list                     = field(default_factory=list)
    current_trade: Optional[Trade]   = None
    cooldown_remaining: int          = 0
    candles_since_entry: int         = 0
    stop_loss_price: float           = 0.0
    peak_price: float                = 0.0
    had_green_candle: bool           = False


class SpikeStrategy:
    def __init__(self, config: StrategyConfig = None):
        self.config      = config or StrategyConfig()
        self.states:     dict[str, SymbolState] = {}
        self.all_trades: list[Trade]            = []
        self.log_entries: list[dict]            = []
        self._lock = threading.Lock()

    # ── 筛选 ──────────────────────────────────
    def should_monitor(self, ticker: dict) -> bool:
        symbol = ticker.get("symbol", "")
        if not symbol.endswith("USDT"):
            return False
        cfg = self.config
        if symbol in cfg.symbol_blacklist:
            return False
        base = symbol[:-4]
        if any(kw in base for kw in cfg.name_blacklist):
            return False
        try:
            gain   = float(ticker["priceChangePercent"])
            volume = float(ticker["quoteVolume"])
        except (KeyError, ValueError):
            return False
        return gain >= cfg.min_gain_24h and volume >= cfg.min_volume_usdt

    # ── 挂单 ──────────────────────────────────
    def should_place_orders(self, candle: dict) -> bool:
        open_p  = float(candle["open"])
        close_p = float(candle["close"])
        if close_p >= open_p:
            return False
        if (open_p - close_p) / open_p * 100 < self.config.min_candle_drop_pct:
            return False
        prev_close = float(candle.get("prev_close", open_p))
        return close_p < prev_close

    def calc_order_prices(self, low: float) -> list[tuple[float, float]]:
        cfg = self.config
        return [
            (low * (1 - cfg.order_pct_1 / 100), cfg.order_ratio_1),
            (low * (1 - cfg.order_pct_2 / 100), cfg.order_ratio_2),
            (low * (1 - cfg.order_pct_3 / 100), cfg.order_ratio_3),
        ]

    def build_orders(self, symbol: str, low: float, ts: str) -> list[Order]:
        total  = self.config.position_size_usdt
        orders = []
        for i, (price, ratio) in enumerate(self.calc_order_prices(low)):
            orders.append(Order(
                id=f"{symbol}_{ts}_{i+1}",
                symbol=symbol,
                price=round(price, 8),
                qty=round((total * ratio) / price, 6),
                ratio=ratio,
            ))
        return orders

    # ── 成交检测 ──────────────────────────────
    def check_fills(self, st: SymbolState, candle: dict) -> list[Order]:
        low    = float(candle["low"])
        filled = []
        for o in st.orders:
            if o.status == OrderStatus.PENDING and low <= o.price:
                o.status     = OrderStatus.FILLED
                o.fill_price = o.price
                o.fill_time  = candle.get("close_time", "")
                filled.append(o)
        return filled

    def open_trade(self, symbol: str, filled: list[Order], ts: str) -> Trade:
        total_cost = sum(o.fill_price * o.qty for o in filled)
        total_qty  = sum(o.qty for o in filled)
        avg_price  = total_cost / total_qty
        return Trade(
            id=f"{symbol}_trade_{ts}",
            symbol=symbol,
            entry_price=round(avg_price, 8),
            qty=round(total_qty, 6),
            entry_time=ts,
        )

    # ── 卖出 ──────────────────────────────────
    def check_exit(self, st: SymbolState, candle: dict) -> Optional[tuple[float, str]]:
        trade = st.current_trade
        if not trade:
            return None

        low   = float(candle["low"])
        high  = float(candle["high"])
        close = float(candle["close"])
        open_ = float(candle["open"])
        cfg   = self.config

        # 统一递增（无论哪个分支出场，hold_candles都正确）
        st.candles_since_entry += 1
        if high > st.peak_price:
            st.peak_price = high

        # 1. 止损
        if low <= st.stop_loss_price:
            return (st.stop_loss_price, "止损")

        # 2. 超时
        if st.candles_since_entry >= cfg.max_hold_candles:
            return (close, "超时出局")

        # 3. 快速止盈
        if (high - trade.entry_price) / trade.entry_price * 100 >= cfg.take_profit_pct:
            st.stop_loss_price = trade.entry_price  # 止损上移保本
            if close >= trade.entry_price * (1 + cfg.take_profit_pct / 100):
                return (close, f"止盈{cfg.take_profit_pct}%")

        # 4. 阴线跟踪止盈
        if close > trade.entry_price:
            st.had_green_candle = True
            if st.stop_loss_price < trade.entry_price:
                st.stop_loss_price = trade.entry_price  # 上移保本

        if st.had_green_candle and cfg.trailing_sell_on_red:
            if close < open_ and (open_ - close) / open_ * 100 >= cfg.trailing_red_min_pct:
                return (close, "回弹后阴线")

        return None

    # ── 主入口 ────────────────────────────────
    def on_candle_close(self, symbol: str, candle: dict) -> dict:
        """
        每根1分钟K线收盘后调用。
        candle需包含: open, high, low, close, volume, close_time, prev_close
        """
        with self._lock:
            if symbol not in self.states:
                self.states[symbol] = SymbolState(symbol=symbol)
            st     = self.states[symbol]
            ts     = candle.get("close_time", datetime.now().isoformat())
            events: list[str] = []

            # 1. 有持仓 → 检查出场
            if st.current_trade:
                result = self.check_exit(st, candle)
                if result:
                    exit_price, reason = result
                    t             = st.current_trade
                    t.exit_price  = exit_price
                    t.exit_time   = ts
                    t.exit_reason = reason
                    t.status      = TradeStatus.CLOSED
                    t.pnl_pct     = round((exit_price - t.entry_price) / t.entry_price * 100, 4)
                    t.pnl_usdt    = round((exit_price - t.entry_price) * t.qty, 4)
                    t.hold_candles = st.candles_since_entry
                    self.all_trades.append(t)
                    events.append(
                        f"出场:{reason} @{exit_price:.6f} "
                        f"PnL={t.pnl_pct:+.2f}% ({t.pnl_usdt:+.4f}U)"
                    )
                    self._reset_state(st)
                # 有持仓期间不评估新挂单
                self._log(symbol, ts, candle, events)
                return self._summary(symbol, st, events)

            # 2. 冷却倒计时
            if st.cooldown_remaining > 0:
                st.cooldown_remaining -= 1
                self._log(symbol, ts, candle, events)
                return self._summary(symbol, st, events)

            # 3. 检查上一根K线的挂单是否在本K线成交
            if st.orders:
                filled = self.check_fills(st, candle)
                if filled:
                    trade             = self.open_trade(symbol, filled, ts)
                    st.current_trade  = trade
                    st.stop_loss_price = round(
                        trade.entry_price * (1 - self.config.stop_loss_pct / 100), 8)
                    st.peak_price          = trade.entry_price
                    st.candles_since_entry = 0
                    events.append(
                        f"入场:{len(filled)}档成交 "
                        f"均价={trade.entry_price:.6f} "
                        f"止损={st.stop_loss_price:.6f}"
                    )
                # 撤销所有剩余挂单
                cancelled = 0
                for o in st.orders:
                    if o.status == OrderStatus.PENDING:
                        o.status = OrderStatus.CANCELLED
                        cancelled += 1
                if cancelled:
                    events.append(f"撤单:{cancelled}档")
                st.orders = []

                if st.current_trade:
                    self._log(symbol, ts, candle, events)
                    return self._summary(symbol, st, events)

            # 4. 评估挂新单
            if self.should_place_orders(candle):
                low        = float(candle["low"])
                st.orders  = self.build_orders(symbol, low, ts)
                pcts       = [self.config.order_pct_1,
                              self.config.order_pct_2,
                              self.config.order_pct_3]
                price_str  = " | ".join(
                    f"{o.price:.6f}(-{p}%)"
                    for o, p in zip(st.orders, pcts)
                )
                events.append(f"挂单:基准低价={low:.6f} → {price_str}")

            self._log(symbol, ts, candle, events)
            return self._summary(symbol, st, events)

    # ── 统计 ──────────────────────────────────
    def get_stats(self) -> dict:
        trades = [t for t in self.all_trades if t.status == TradeStatus.CLOSED]
        if not trades:
            return {"total": 0}
        wins   = [t for t in trades if t.pnl_pct > 0]
        losses = [t for t in trades if t.pnl_pct <= 0]
        return {
            "total":     len(trades),
            "wins":      len(wins),
            "losses":    len(losses),
            "win_rate":  round(len(wins) / len(trades) * 100, 1),
            "total_pnl": round(sum(t.pnl_usdt for t in trades), 4),
            "avg_win":   round(sum(t.pnl_pct for t in wins)   / len(wins),   2) if wins   else 0,
            "avg_loss":  round(sum(t.pnl_pct for t in losses) / len(losses), 2) if losses else 0,
            "max_win":   round(max(t.pnl_pct for t in trades), 2),
            "max_loss":  round(min(t.pnl_pct for t in trades), 2),
            "avg_hold":  round(sum(t.hold_candles for t in trades) / len(trades), 1),
        }

    def get_trades_json(self) -> list[dict]:
        result = []
        for t in self.all_trades:
            d = asdict(t)
            d["status"] = t.status.value
            result.append(d)
        return result

    def get_logs(self) -> list[dict]:
        return list(self.log_entries)

    # ── 内部工具 ──────────────────────────────
    def _reset_state(self, st: SymbolState) -> None:
        st.current_trade       = None
        st.candles_since_entry = 0
        st.had_green_candle    = False
        st.peak_price          = 0.0
        st.stop_loss_price     = 0.0
        st.cooldown_remaining  = self.config.cooldown_candles
        for o in st.orders:
            if o.status == OrderStatus.PENDING:
                o.status = OrderStatus.CANCELLED
        st.orders = []

    def _log(self, symbol: str, ts: str, candle: dict, events: list[str]) -> None:
        if events:
            self.log_entries.append({
                "time":   ts,
                "symbol": symbol,
                "close":  float(candle["close"]),
                "events": events,
            })

    def _summary(self, symbol: str, st: SymbolState, events: list[str]) -> dict:
        trade_info = None
        if st.current_trade:
            t = st.current_trade
            trade_info = {
                "entry_price":  t.entry_price,
                "qty":          t.qty,
                "entry_time":   t.entry_time,
                "stop_loss":    st.stop_loss_price,
                "hold_candles": st.candles_since_entry,
                "peak_price":   st.peak_price,
            }
        return {
            "symbol":    symbol,
            "has_orders": sum(1 for o in st.orders if o.status == OrderStatus.PENDING),
            "in_trade":  st.current_trade is not None,
            "trade":     trade_info,
            "cooldown":  st.cooldown_remaining,
            "events":    events,
        }
