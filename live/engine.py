"""
实盘策略引擎 v2.0
- paper 模式：模拟成交，不调币安下单API
- live  模式：真实下单，调币安签名API
- 重启自动恢复：从 state.json 恢复持仓/挂单，同步币安实际状态
"""

import time
import threading
import logging
from datetime import datetime, timezone
from typing import Optional

from binance_client import BinanceClient
from store import (
    load_config, save_config, load_state, save_state,
    load_trades, append_trade, append_log, read_recent_logs,
)

logger = logging.getLogger(__name__)

BL_EXACT = {"USDCUSDT","BUSDUSDT","TUSDUSDT","FDUSDUSDT","DAIUSDT"}
BL_KW    = ("UP","DOWN","BULL","BEAR","3L","3S")

SCAN_INTERVAL  = 15 * 60   # 15分钟扫描一次
KLINE_INTERVAL = 60        # 每分钟触发一次


class LiveEngine:
    def __init__(self):
        self.cfg     = load_config()
        self.state   = load_state()
        self.client  = self._make_client()
        self._lock   = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        # 内存日志（最近200条，供API快速返回）
        self._mem_logs: list[dict] = read_recent_logs(200)

    def _make_client(self) -> Optional[BinanceClient]:
        key    = self.cfg.get("api_key", "")
        secret = self.cfg.get("api_secret", "")
        if key and secret:
            return BinanceClient(key, secret)
        return None

    # ── 公开方法（供 Flask 调用） ─────────────────────

    def get_status(self) -> dict:
        with self._lock:
            trades  = load_trades()
            closed  = [t for t in trades if t.get("status") == "closed"]
            wins    = [t for t in closed if t.get("pnl_pct", 0) > 0]
            wr      = round(len(wins)/len(closed)*100, 1) if closed else 0
            return {
                "running":    self.state["running"],
                "mode":       self.cfg.get("mode", "paper"),
                "symbols":    self.state["symbols"],
                "positions":  self.state["positions"],
                "orders":     self.state["orders"],
                "pnl_total":  self.state["pnl_total"],
                "pnl_log":    self.state["pnl_log"][-80:],
                "trade_count": len(closed),
                "win_rate":   wr,
                "logs":       self._mem_logs[:100],
            }

    def get_trades(self) -> list:
        return load_trades()

    def get_config(self) -> dict:
        from store import get_config_safe
        return get_config_safe(self.cfg)

    def update_config(self, new_cfg: dict) -> dict:
        """更新配置并持久化"""
        with self._lock:
            # 保护：不允许前端清空 api_secret（前端传 "***" 时保留原值）
            if new_cfg.get("api_secret") == "***":
                new_cfg["api_secret"] = self.cfg.get("api_secret", "")
            self.cfg.update(new_cfg)
            save_config(self.cfg)
            # 重建客户端（Key可能变了）
            self.client = self._make_client()
        self._log("SYS", "配置已更新并保存")
        return {"ok": True}

    def start(self) -> dict:
        with self._lock:
            if self.state["running"]:
                return {"ok": False, "msg": "已在运行"}
            self.state["running"] = True
            save_state(self.state)
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._main_loop, daemon=True)
        self._thread.start()
        self._log("SYS", f"策略启动 模式={self.cfg.get('mode','paper')}")
        return {"ok": True}

    def stop(self) -> dict:
        self._stop_event.set()
        with self._lock:
            self.state["running"] = False
            save_state(self.state)
        self._log("SYS", "策略已停止")
        return {"ok": True}

    def reset(self) -> dict:
        """重置所有数据（仅paper模式允许）"""
        if self.cfg.get("mode") == "live":
            return {"ok": False, "msg": "实盘模式不允许重置，请先切换到虚拟盘"}
        self.stop()
        with self._lock:
            self.state = {
                "running": False, "symbols": [],
                "positions": {}, "orders": {},
                "pnl_total": 0.0, "pnl_log": [],
            }
            save_state(self.state)
            from store import save_trades
            save_trades([])
        self._mem_logs = []
        self._log("SYS", "已重置")
        return {"ok": True}

    def manual_scan(self) -> dict:
        """手动触发扫描"""
        symbols = self._scan_gainers()
        return {"ok": True, "symbols": symbols, "count": len(symbols)}

    def add_symbol(self, symbol: str) -> dict:
        symbol = symbol.upper()
        with self._lock:
            if symbol not in self.state["symbols"]:
                self.state["symbols"].append(symbol)
                save_state(self.state)
        self._log("SYS", f"手动添加监控: {symbol}")
        return {"ok": True}

    def remove_symbol(self, symbol: str) -> dict:
        symbol = symbol.upper()
        with self._lock:
            if symbol in self.state["positions"]:
                return {"ok": False, "msg": f"{symbol} 有持仓，不能移除"}
            if symbol in self.state["symbols"]:
                self.state["symbols"].remove(symbol)
                save_state(self.state)
        self._log("SYS", f"移除监控: {symbol}")
        return {"ok": True}

    # ── 主循环 ────────────────────────────────────────

    def _main_loop(self):
        last_scan = 0
        self._log("SYS", "主循环启动，等待K线整点...")

        # 等待下一个整分钟（+3秒缓冲）
        now    = time.time()
        remain = 60 - (now % 60) + 3
        if self._stop_event.wait(remain):
            return

        while not self._stop_event.is_set():
            tick_start = time.time()

            # 每15分钟扫描涨幅榜
            if tick_start - last_scan >= SCAN_INTERVAL:
                self._scan_gainers()
                last_scan = tick_start

            # 处理每个监控币种
            symbols = list(self.state.get("symbols", []))
            for sym in symbols:
                if self._stop_event.is_set():
                    break
                try:
                    self._process_symbol(sym)
                except Exception as e:
                    self._log(sym, f"处理异常: {e}")
                time.sleep(0.3)

            # 等待下一分钟整点
            elapsed = time.time() - tick_start
            wait    = max(0, KLINE_INTERVAL - elapsed)
            self._stop_event.wait(wait)

    # ── 涨幅榜扫描 ────────────────────────────────────

    def _scan_gainers(self) -> list:
        try:
            data = self.client.get_ticker_24h() if self.client else self._public_ticker()
            cfg  = self.cfg
            gainers = []
            for t in data:
                sym = t.get("symbol","")
                if not sym.endswith("USDT"): continue
                if sym in BL_EXACT: continue
                base = sym[:-4]
                if any(k in base for k in BL_KW): continue
                try:
                    gain = float(t["priceChangePercent"])
                    vol  = float(t["quoteVolume"])
                except: continue
                if gain >= cfg["min_gain_24h"] and vol >= cfg["min_volume_usdt"]:
                    gainers.append(sym)

            gainers.sort(key=lambda s: float(
                next(t["priceChangePercent"] for t in data if t["symbol"]==s)
            ), reverse=True)
            gainers = gainers[:15]

            with self._lock:
                # 新增
                added = [s for s in gainers if s not in self.state["symbols"]]
                # 移除不再符合条件的（保留有持仓的）
                keep = [s for s in self.state["symbols"]
                        if s in gainers or s in self.state["positions"]]
                self.state["symbols"] = keep
                for s in added:
                    if s not in self.state["symbols"]:
                        self.state["symbols"].append(s)
                save_state(self.state)

            if added:
                self._log("SYS", f"扫描完成 新增{len(added)}个: {','.join(added[:5])}")
            else:
                self._log("SYS", f"扫描完成 监控{len(self.state['symbols'])}个币种")
            return self.state["symbols"]
        except Exception as e:
            self._log("SYS", f"扫描失败: {e}")
            return self.state.get("symbols", [])

    def _public_ticker(self) -> list:
        import urllib.request, json
        with urllib.request.urlopen(
            "https://api.binance.com/api/v3/ticker/24hr", timeout=10
        ) as r:
            return json.loads(r.read())

    # ── 每分钟处理单个币种 ───────────────────────────

    def _process_symbol(self, symbol: str):
        candle = self._get_closed_kline(symbol)
        if not candle:
            return

        mode = self.cfg.get("mode", "paper")
        with self._lock:
            pos    = self.state["positions"].get(symbol)
            orders = self.state["orders"].get(symbol, [])

        # 1. 有持仓 → 检查出场
        if pos:
            exit_info = self._check_exit(symbol, pos, candle)
            if exit_info:
                self._do_exit(symbol, pos, exit_info, candle, mode)
            return

        # 2. 检查挂单是否成交
        if orders:
            if mode == "live":
                filled = self._sync_orders_live(symbol, orders)
            else:
                filled = self._sync_orders_paper(orders, candle)

            if filled:
                self._open_position(symbol, filled, candle, mode)

            # 撤销剩余未成交挂单
            self._cancel_pending_orders(symbol, orders, mode)

            with self._lock:
                self.state["orders"][symbol] = []
                save_state(self.state)

            if self.state["positions"].get(symbol):
                return  # 已开仓，本轮不再挂新单

        # 3. 评估挂新单
        max_pos = self.cfg.get("max_open_positions", 3)
        if len(self.state["positions"]) >= max_pos:
            return  # 达到最大持仓数

        if self._should_place_orders(candle):
            self._place_orders(symbol, candle, mode)

    # ── K线获取 ──────────────────────────────────────

    def _get_closed_kline(self, symbol: str) -> Optional[dict]:
        try:
            kdata  = self.client.get_klines(symbol, "1m", limit=3) if self.client \
                     else self._public_klines(symbol)
            ticker = self.client.get_ticker_24h(symbol) if self.client \
                     else self._public_ticker_single(symbol)
            if len(kdata) < 2:
                return None
            prev = kdata[-3] if len(kdata) >= 3 else kdata[0]
            k    = kdata[-2]   # 已完整收盘
            return {
                "open":       float(k[1]),
                "high":       float(k[2]),
                "low":        float(k[3]),
                "close":      float(k[4]),
                "volume":     float(k[5]),
                "prev_close": float(prev[4]),
                "day_high":   float(ticker.get("highPrice", 0)),
                "close_time": datetime.fromtimestamp(
                    k[6]/1000, tz=timezone.utc
                ).strftime("%H:%M:%S"),
            }
        except Exception as e:
            self._log(symbol, f"K线获取失败: {e}")
            return None

    def _public_klines(self, symbol: str) -> list:
        import urllib.request, json
        url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=1m&limit=3"
        with urllib.request.urlopen(url, timeout=10) as r:
            return json.loads(r.read())

    def _public_ticker_single(self, symbol: str) -> dict:
        import urllib.request, json
        url = f"https://api.binance.com/api/v3/ticker/24hr?symbol={symbol}"
        with urllib.request.urlopen(url, timeout=10) as r:
            return json.loads(r.read())

    # ── 挂单条件 ─────────────────────────────────────

    def _should_place_orders(self, candle: dict) -> bool:
        if candle["close"] >= candle["open"]:
            return False
        dh = candle.get("day_high", 0)
        if dh > 0 and candle["close"] < dh * self.cfg["high_price_ratio"]:
            return False
        return True

    # ── 下单（三档限价买） ───────────────────────────

    def _place_orders(self, symbol: str, candle: dict, mode: str):
        cfg   = self.cfg
        close = candle["close"]
        total = cfg["position_size_usdt"]
        slots = [
            (close * (1 - cfg["order_pct_1"]/100), cfg["order_ratio_1"]),
            (close * (1 - cfg["order_pct_2"]/100), cfg["order_ratio_2"]),
            (close * (1 - cfg["order_pct_3"]/100), cfg["order_ratio_3"]),
        ]
        new_orders = []
        for i, (price, ratio) in enumerate(slots):
            qty = (total * ratio) / price
            order_rec = {
                "tier":         i + 1,
                "price":        round(price, 8),
                "qty":          round(qty, 6),
                "ratio":        ratio,
                "status":       "pending",
                "binance_id":   None,
                "client_id":    f"{symbol}_{candle['close_time']}_{i+1}",
            }
            if mode == "live" and self.client:
                try:
                    resp = self.client.place_limit_buy(
                        symbol, price, qty, order_rec["client_id"]
                    )
                    order_rec["binance_id"] = resp.get("orderId")
                    order_rec["status"]     = "pending"
                except Exception as e:
                    self._log(symbol, f"下单失败 档{i+1}: {e}")
                    continue
            new_orders.append(order_rec)

        if new_orders:
            prices = " / ".join(f"{o['price']:.6f}(-{p[0]}%)"
                                for o, p in zip(new_orders,
                                [cfg['order_pct_1'], cfg['order_pct_2'], cfg['order_pct_3']]))
            self._log(symbol, f"挂单 收盘={close:.6f} → {prices}")
            with self._lock:
                self.state["orders"][symbol] = new_orders
                save_state(self.state)

    # ── 成交检测 ─────────────────────────────────────

    def _sync_orders_paper(self, orders: list, candle: dict) -> list:
        """虚拟盘：用K线最低价判断是否成交"""
        filled = []
        for o in orders:
            if o["status"] == "pending" and candle["low"] <= o["price"]:
                o["status"]     = "filled"
                o["fill_price"] = o["price"]
                filled.append(o)
        return filled

    def _sync_orders_live(self, symbol: str, orders: list) -> list:
        """实盘：查询币安实际成交状态"""
        filled = []
        for o in orders:
            if o["status"] != "pending" or not o.get("binance_id"):
                continue
            try:
                resp = self.client.get_order(symbol, o["binance_id"])
                if resp["status"] == "FILLED":
                    o["status"]     = "filled"
                    o["fill_price"] = float(resp["price"])
                    filled.append(o)
                elif resp["status"] in ("CANCELED","EXPIRED","REJECTED"):
                    o["status"] = "cancelled"
            except Exception as e:
                self._log(symbol, f"查询订单失败: {e}")
        return filled

    # ── 开仓 ─────────────────────────────────────────

    def _open_position(self, symbol: str, filled: list, candle: dict, mode: str):
        tc  = sum(o["fill_price"] * o["qty"] for o in filled)
        tq  = sum(o["qty"] for o in filled)
        avg = tc / tq
        pos = {
            "entry_price": round(avg, 8),
            "qty":         round(tq, 6),
            "entry_time":  candle["close_time"],
            "stop_loss":   round(avg * (1 - self.cfg["stop_loss_pct"]/100), 8),
            "peak":        avg,
            "had_green":   False,
            "hold":        0,
        }
        with self._lock:
            self.state["positions"][symbol] = pos
            save_state(self.state)
        self._log(symbol,
            f"入场 {len(filled)}档成交 均价={avg:.6f} 止损={pos['stop_loss']:.6f}")

    # ── 出场判断 ─────────────────────────────────────

    def _check_exit(self, symbol: str, pos: dict, candle: dict) -> Optional[dict]:
        cfg   = self.cfg
        pos["hold"] = pos.get("hold", 0) + 1
        if candle["high"] > pos.get("peak", 0):
            pos["peak"] = candle["high"]

        # 1. 止损
        if candle["low"] <= pos["stop_loss"]:
            return {"price": pos["stop_loss"], "reason": "止损", "type": "market"}

        # 2. 超时
        if pos["hold"] >= cfg["max_hold_candles"]:
            return {"price": candle["close"], "reason": "超时出局", "type": "market"}

        # 3. 快速止盈
        gain = (candle["high"] - pos["entry_price"]) / pos["entry_price"] * 100
        if gain >= cfg["take_profit_pct"]:
            pos["stop_loss"] = pos["entry_price"]  # 保本
            if candle["close"] >= pos["entry_price"] * (1 + cfg["take_profit_pct"]/100):
                return {"price": candle["close"],
                        "reason": f"止盈{cfg['take_profit_pct']}%", "type": "limit"}

        # 4. 阴线跟踪止盈
        if candle["close"] > pos["entry_price"]:
            pos["had_green"] = True
            pos["stop_loss"] = max(pos["stop_loss"], pos["entry_price"])
        if pos.get("had_green") and candle["close"] < candle["open"]:
            drop = (candle["open"] - candle["close"]) / candle["open"] * 100
            if drop >= cfg.get("trailing_red_min_pct", 1.0):
                return {"price": candle["close"], "reason": "回弹后阴线", "type": "market"}

        # 更新持仓状态
        with self._lock:
            self.state["positions"][symbol] = pos
            save_state(self.state)
        return None

    # ── 出场执行 ─────────────────────────────────────

    def _do_exit(self, symbol: str, pos: dict, exit_info: dict,
                 candle: dict, mode: str):
        exit_price = exit_info["price"]
        reason     = exit_info["reason"]

        if mode == "live" and self.client:
            try:
                self.client.place_market_sell(symbol, pos["qty"])
                # 用实际成交价（近似用当前价）
            except Exception as e:
                self._log(symbol, f"卖出失败: {e}")
                return

        pnl_pct  = round((exit_price - pos["entry_price"]) / pos["entry_price"] * 100, 4)
        pnl_usdt = round((exit_price - pos["entry_price"]) * pos["qty"], 4)
        trade = {
            "symbol":      symbol,
            "entry_price": pos["entry_price"],
            "exit_price":  exit_price,
            "qty":         pos["qty"],
            "entry_time":  pos["entry_time"],
            "exit_time":   candle["close_time"],
            "exit_reason": reason,
            "hold_candles": pos["hold"],
            "pnl_pct":     pnl_pct,
            "pnl_usdt":    pnl_usdt,
            "status":      "closed",
            "mode":        mode,
        }
        append_trade(trade)

        with self._lock:
            del self.state["positions"][symbol]
            self.state["pnl_total"] = round(
                self.state.get("pnl_total", 0) + pnl_usdt, 4)
            log = self.state.get("pnl_log", [])
            log.append(pnl_pct)
            if len(log) > 200:
                log = log[-200:]
            self.state["pnl_log"] = log
            save_state(self.state)

        self._log(symbol,
            f"出场:{reason} 均价={exit_price:.6f} "
            f"PnL={pnl_pct:+.2f}% ({pnl_usdt:+.4f}U)")

    # ── 撤单 ─────────────────────────────────────────

    def _cancel_pending_orders(self, symbol: str, orders: list, mode: str):
        cancelled = 0
        for o in orders:
            if o["status"] == "pending":
                if mode == "live" and self.client and o.get("binance_id"):
                    try:
                        self.client.cancel_order(symbol, o["binance_id"])
                    except Exception:
                        pass
                o["status"] = "cancelled"
                cancelled  += 1
        if cancelled:
            self._log(symbol, f"撤单 {cancelled}档未成交")

    # ── 日志 ─────────────────────────────────────────

    def _log(self, symbol: str, msg: str):
        append_log(symbol, msg)
        entry = {
            "time":   datetime.now().strftime("%H:%M:%S"),
            "symbol": symbol,
            "msg":    msg,
        }
        self._mem_logs.insert(0, entry)
        if len(self._mem_logs) > 400:
            self._mem_logs = self._mem_logs[:400]


# ── 单例 ─────────────────────────────────────────────
_engine: Optional[LiveEngine] = None

def get_engine() -> LiveEngine:
    global _engine
    if _engine is None:
        _engine = LiveEngine()
    return _engine
