"""
持久化层
所有数据写入 data/ 目录下的 JSON 文件
重启后自动从文件恢复
"""

import json
import os
import threading
from datetime import datetime
from typing import Any

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
os.makedirs(DATA_DIR, exist_ok=True)

CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
TRADES_FILE = os.path.join(DATA_DIR, "trades.json")
STATE_FILE  = os.path.join(DATA_DIR, "state.json")
LOG_FILE    = os.path.join(DATA_DIR, "bot.log")

_lock = threading.Lock()


# ── 工具 ──────────────────────────────────────────────

def _read(path: str, default: Any) -> Any:
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return default


def _write(path: str, data: Any) -> None:
    with _lock:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)
        os.replace(tmp, path)   # 原子写入，防止写一半时崩溃


# ── 默认配置 ─────────────────────────────────────────

DEFAULT_CONFIG = {
    "min_gain_24h":      30.0,
    "min_volume_usdt":   500000.0,
    "high_price_ratio":  0.80,
    "order_pct_1":       4.0,
    "order_pct_2":       6.0,
    "order_pct_3":       9.0,
    "order_ratio_1":     0.40,
    "order_ratio_2":     0.35,
    "order_ratio_3":     0.25,
    "stop_loss_pct":     3.0,
    "take_profit_pct":   5.0,
    "trailing_sell_on_red": True,
    "trailing_red_min_pct": 1.0,
    "max_hold_candles":  10,
    "cooldown_candles":  2,
    "position_size_usdt": 100.0,
    # 实盘专用
    "mode":              "paper",   # "paper" | "live"
    "api_key":           "",
    "api_secret":        "",
    "max_open_positions": 3,        # 同时最多持仓币种数
    # WebSocket秒级持仓参数
    "ws_stop_loss_pct":    1.5,     # 止损%（相对买入均价）
    "ws_take_profit_pct":  2.5,     # 止盈%
    "ws_max_hold_seconds": 5,       # 最大持仓秒数
}


# ── Config ───────────────────────────────────────────

def load_config() -> dict:
    stored = _read(CONFIG_FILE, {})
    cfg    = {**DEFAULT_CONFIG, **stored}   # 新字段自动补默认值
    return cfg


def save_config(cfg: dict) -> None:
    # 不存储敏感字段到文件（API Key 单独处理）
    _write(CONFIG_FILE, cfg)


def get_config_safe(cfg: dict) -> dict:
    """返回给前端的配置（隐藏 secret）"""
    safe = dict(cfg)
    if safe.get("api_secret"):
        safe["api_secret"] = "***"
    return safe


# ── Trades ───────────────────────────────────────────

def load_trades() -> list:
    return _read(TRADES_FILE, [])


def save_trades(trades: list) -> None:
    _write(TRADES_FILE, trades)


def append_trade(trade: dict) -> None:
    trades = load_trades()
    trades.insert(0, trade)
    if len(trades) > 500:
        trades = trades[:500]
    save_trades(trades)


# ── Runtime State ────────────────────────────────────
# 保存运行时状态：监控币种、持仓、挂单（币安 orderId）
# 重启后用于恢复 → 自动重新同步币安实际状态

DEFAULT_STATE = {
    "running":   False,
    "symbols":   [],        # 当前监控币种列表
    "positions": {},        # symbol -> {entry_price, qty, stop_loss, ...}
    "orders":    {},        # symbol -> [{binance_order_id, price, qty, ...}]
    "pnl_total": 0.0,
    "pnl_log":   [],
}


def load_state() -> dict:
    stored = _read(STATE_FILE, {})
    return {**DEFAULT_STATE, **stored}


def save_state(state: dict) -> None:
    _write(STATE_FILE, state)


# ── Log ──────────────────────────────────────────────

def append_log(symbol: str, msg: str) -> None:
    from datetime import timezone as _tz
    ts   = datetime.now(_tz.utc).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [{symbol}] {msg}\n"
    with _lock:
        try:
            if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > 10*1024*1024:
                with open(LOG_FILE,"r",encoding="utf-8",errors="replace") as f:
                    lines = f.readlines()
                with open(LOG_FILE,"w",encoding="utf-8") as f:
                    f.writelines(lines[len(lines)//2:])
        except Exception:
            pass
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line)


def read_recent_logs(n: int = 200) -> list[dict]:
    """读取最近 n 行日志，返回结构化列表"""
    if not os.path.exists(LOG_FILE):
        return []
    try:
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
        result = []
        for line in lines[-n:]:
            line = line.strip()
            if not line:
                continue
            # 格式: [2026-04-14 12:00:00] [BTCUSDT] 挂单...
            try:
                ts_end  = line.index("]")
                ts      = line[1:ts_end]
                rest    = line[ts_end+3:]
                sym_end = rest.index("]")
                sym     = rest[1:sym_end]
                msg     = rest[sym_end+2:]
                result.append({"time": ts[-8:], "symbol": sym, "msg": msg})
            except Exception:
                result.append({"time": "--:--:--", "symbol": "SYS", "msg": line})
        return list(reversed(result))
    except Exception:
        return []
