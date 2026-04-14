"""
回测引擎
- 从币安API拉取历史1分钟K线
- 模拟筛选流程 + 策略执行
- 输出回测报告JSON
"""

import json
import time
import urllib.request
from datetime import datetime, timezone
from strategy import SpikeStrategy, StrategyConfig


# ─────────────────────────────────────────────
# 币安API（无需签名的公开接口）
# ─────────────────────────────────────────────

BASE = "https://api.binance.com"

def fetch_url(url: str) -> dict | list:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())

def get_top_gainers(min_gain: float = 50.0, min_volume: float = 500_000,
                    limit: int = 10) -> list[dict]:
    """获取24h涨幅排行"""
    data = fetch_url(f"{BASE}/api/v3/ticker/24hr")
    results = []
    for t in data:
        sym = t["symbol"]
        if not sym.endswith("USDT"):
            continue
        blacklist = ["UPUSDT", "DOWNUSDT", "BUSDUSDT", "USDCUSDT", "TUSDUSDT"]
        if any(b in sym for b in blacklist):
            continue
        try:
            gain = float(t["priceChangePercent"])
            vol  = float(t["quoteVolume"])
        except:
            continue
        if gain >= min_gain and vol >= min_volume:
            results.append({
                "symbol": sym,
                "gain":   round(gain, 2),
                "volume": round(vol, 0),
                "price":  float(t["lastPrice"]),
            })
    results.sort(key=lambda x: x["gain"], reverse=True)
    return results[:limit]

def get_klines(symbol: str, interval: str = "1m",
               limit: int = 500, start_ms: int = None, end_ms: int = None) -> list[dict]:
    """获取K线数据"""
    url = f"{BASE}/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    if start_ms:
        url += f"&startTime={start_ms}"
    if end_ms:
        url += f"&endTime={end_ms}"
    raw = fetch_url(url)
    candles = []
    for i, k in enumerate(raw):
        candles.append({
            "open_time":  k[0],
            "open":       float(k[1]),
            "high":       float(k[2]),
            "low":        float(k[3]),
            "close":      float(k[4]),
            "volume":     float(k[5]),
            "close_time": datetime.fromtimestamp(k[6]/1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "prev_close": float(raw[i-1][4]) if i > 0 else float(k[1]),
        })
    return candles


# ─────────────────────────────────────────────
# 回测主逻辑
# ─────────────────────────────────────────────

def run_backtest(symbol: str, candles: list[dict],
                 config: StrategyConfig = None) -> dict:
    """对单个币种运行回测"""
    strategy = SpikeStrategy(config or StrategyConfig())
    results = []
    for candle in candles:
        summary = strategy.on_candle_close(symbol, candle)
        if summary["events"]:
            results.append({
                "time":   candle["close_time"],
                "close":  candle["close"],
                "events": summary["events"],
            })
    stats  = strategy.get_stats()
    trades = strategy.get_trades_json()
    return {
        "symbol":  symbol,
        "candles": len(candles),
        "stats":   stats,
        "trades":  trades,
        "log":     results,
    }


def run_backtest_multi(symbols: list[str], days: int = 3,
                       config: StrategyConfig = None) -> dict:
    """对多个币种并发回测"""
    end_ms   = int(time.time() * 1000)
    start_ms = end_ms - days * 24 * 60 * 60 * 1000
    all_results = {}
    for sym in symbols:
        print(f"回测 {sym} ...")
        try:
            candles = get_klines(sym, "1m", limit=1000,
                                 start_ms=start_ms, end_ms=end_ms)
            result  = run_backtest(sym, candles, config)
            all_results[sym] = result
            time.sleep(0.3)  # 避免频率限制
        except Exception as e:
            all_results[sym] = {"error": str(e)}

    # 汇总统计
    all_trades = []
    for r in all_results.values():
        if "trades" in r:
            all_trades.extend(r["trades"])

    closed = [t for t in all_trades if t["status"] == "closed"]
    wins   = [t for t in closed if t["pnl_pct"] > 0]
    summary = {
        "total_trades": len(closed),
        "win_rate":     round(len(wins) / len(closed) * 100, 1) if closed else 0,
        "total_pnl":    round(sum(t["pnl_usdt"] for t in closed), 2),
        "symbols":      list(all_results.keys()),
    }
    return {"summary": summary, "details": all_results}


# ─────────────────────────────────────────────
# CLI 入口
# ─────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    import argparse

    parser = argparse.ArgumentParser(description="插针策略回测")
    parser.add_argument("--symbol",  default=None,  help="指定币种如 BTCUSDT，不填则自动筛选")
    parser.add_argument("--days",    type=int, default=3, help="回测天数")
    parser.add_argument("--gain",    type=float, default=50.0, help="最低24h涨幅%")
    parser.add_argument("--output",  default="backtest_result.json", help="输出文件")
    args = parser.parse_args()

    config = StrategyConfig(min_gain_24h=args.gain)

    if args.symbol:
        symbols = [args.symbol.upper()]
    else:
        print("筛选涨幅榜...")
        gainers = get_top_gainers(min_gain=args.gain)
        symbols = [g["symbol"] for g in gainers[:5]]
        print(f"筛选到: {symbols}")

    result = run_backtest_multi(symbols, days=args.days, config=config)

    with open(args.output, "w") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, default=str)

    print(f"\n===== 回测完成 =====")
    s = result["summary"]
    print(f"总交易: {s['total_trades']}  胜率: {s['win_rate']}%  总盈亏: ${s['total_pnl']}")
    print(f"结果已保存到 {args.output}")
