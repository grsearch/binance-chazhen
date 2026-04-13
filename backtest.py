"""
回测引擎 v1.1
- 从币安API拉取真实历史1分钟K线（无需API Key）
- 支持指定币种或自动从涨幅榜筛选
- 分页拉取突破单次1000根限制
- 输出JSON报告

用法：
  python backtest.py --symbol BTCUSDT --days 3
  python backtest.py --days 7 --gain 50 --output result.json
"""

import json
import time
import argparse
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from strategy import SpikeStrategy, StrategyConfig


BASE_URL = "https://api.binance.com"
KLINE_LIMIT = 1000          # 单次最多拉取根数
CANDLES_PER_DAY = 1440      # 1分钟K线每天1440根


def _fetch(url: str):
    req = urllib.request.Request(
        url, headers={"User-Agent": "python-spike-bot/1.1"}
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())


# ─────────────────────────────────────────────
# 数据获取
# ─────────────────────────────────────────────

def get_top_gainers(
    min_gain: float = 50.0,
    min_volume: float = 500_000.0,
    limit: int = 10,
) -> list[dict]:
    """获取24h涨幅榜（现货USDT对）"""
    data = _fetch(f"{BASE_URL}/api/v3/ticker/24hr")
    blacklist_exact = {"USDCUSDT", "BUSDUSDT", "TUSDUSDT", "FDUSDUSDT", "DAIUSDT"}
    blacklist_kw    = ("UP", "DOWN", "BULL", "BEAR", "3L", "3S")
    results = []
    for t in data:
        sym = t["symbol"]
        if not sym.endswith("USDT"):
            continue
        if sym in blacklist_exact:
            continue
        base = sym[:-4]
        if any(kw in base for kw in blacklist_kw):
            continue
        try:
            gain = float(t["priceChangePercent"])
            vol  = float(t["quoteVolume"])
        except (KeyError, ValueError):
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


def get_klines(
    symbol: str,
    start_ms: int,
    end_ms: int,
    interval: str = "1m",
) -> list[dict]:
    """
    分页拉取K线，突破单次1000根限制。
    返回按时间升序排列的K线列表。
    """
    all_raw = []
    current_start = start_ms

    while current_start < end_ms:
        url = (
            f"{BASE_URL}/api/v3/klines"
            f"?symbol={symbol}&interval={interval}"
            f"&startTime={current_start}&endTime={end_ms}"
            f"&limit={KLINE_LIMIT}"
        )
        batch = _fetch(url)
        if not batch:
            break
        all_raw.extend(batch)
        # 下一批从最后一根的open_time+1ms开始
        current_start = batch[-1][0] + 1
        if len(batch) < KLINE_LIMIT:
            break
        time.sleep(0.2)  # 避免触发频率限制

    candles = []
    for i, k in enumerate(all_raw):
        candles.append({
            "open_time":  k[0],
            "open":       float(k[1]),
            "high":       float(k[2]),
            "low":        float(k[3]),
            "close":      float(k[4]),
            "volume":     float(k[5]),
            "close_time": datetime.fromtimestamp(
                k[6] / 1000, tz=timezone.utc
            ).strftime("%Y-%m-%d %H:%M:%S"),
            # prev_close: 第一根用自身open（无前驱），其余用前根close
            "prev_close": float(all_raw[i - 1][4]) if i > 0 else float(k[1]),
        })
    return candles


# ─────────────────────────────────────────────
# 回测执行
# ─────────────────────────────────────────────

def run_backtest_single(
    symbol: str,
    candles: list[dict],
    config: StrategyConfig = None,
) -> dict:
    """对单个币种跑回测，返回统计 + 逐笔交易"""
    strategy = SpikeStrategy(config or StrategyConfig())
    for candle in candles:
        strategy.on_candle_close(symbol, candle)

    stats  = strategy.get_stats()
    trades = strategy.get_trades_json()
    return {
        "symbol":       symbol,
        "candle_count": len(candles),
        "stats":        stats,
        "trades":       trades,
    }


def run_backtest(
    symbols: list[str],
    days: int = 3,
    config: StrategyConfig = None,
) -> dict:
    """多币种回测，汇总统计"""
    end_ms   = int(time.time() * 1000)
    start_ms = end_ms - days * 24 * 3600 * 1000

    details: dict[str, dict] = {}
    for sym in symbols:
        print(f"  [{sym}] 拉取K线 {days}天 ...", end=" ", flush=True)
        try:
            candles = get_klines(sym, start_ms, end_ms)
            print(f"{len(candles)}根", end=" ")
            result  = run_backtest_single(sym, candles, config)
            details[sym] = result
            st = result["stats"]
            if st.get("total", 0):
                print(f"→ 交易{st['total']}次 胜率{st['win_rate']}% PnL={st['total_pnl']:+.4f}U")
            else:
                print("→ 无交易触发")
        except Exception as e:
            print(f"→ 错误: {e}")
            details[sym] = {"error": str(e)}
        time.sleep(0.3)

    # 汇总
    all_trades = []
    for r in details.values():
        if "trades" in r:
            all_trades.extend(r["trades"])

    closed = [t for t in all_trades if t.get("status") == "closed"]
    wins   = [t for t in closed if t["pnl_pct"] > 0]
    summary = {
        "symbols":      symbols,
        "days":         days,
        "total_trades": len(closed),
        "wins":         len(wins),
        "losses":       len(closed) - len(wins),
        "win_rate":     round(len(wins) / len(closed) * 100, 1) if closed else 0,
        "total_pnl":    round(sum(t["pnl_usdt"] for t in closed), 4),
        "avg_pnl_pct":  round(sum(t["pnl_pct"] for t in closed) / len(closed), 2) if closed else 0,
    }
    return {"summary": summary, "details": details}


# ─────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="插针策略回测 — 使用真实币安历史K线"
    )
    parser.add_argument("--symbol", default=None,
                        help="指定一个或多个币种，逗号分隔，如 BTCUSDT,ETHUSDT")
    parser.add_argument("--days",   type=int,   default=3,
                        help="回测天数（默认3）")
    parser.add_argument("--gain",   type=float, default=50.0,
                        help="自动筛选时的最低24h涨幅%%（默认50）")
    parser.add_argument("--top",    type=int,   default=5,
                        help="自动筛选时取涨幅前N名（默认5）")
    parser.add_argument("--output", default="backtest_result.json",
                        help="结果输出文件（默认 backtest_result.json）")
    # 策略参数覆盖
    parser.add_argument("--sl",   type=float, default=3.0,  help="止损%%")
    parser.add_argument("--tp",   type=float, default=5.0,  help="止盈%%")
    parser.add_argument("--hold", type=int,   default=10,   help="最大持仓K线数")
    parser.add_argument("--size", type=float, default=100.0,help="每次投入USDT")
    args = parser.parse_args()

    config = StrategyConfig(
        min_gain_24h      = args.gain,
        stop_loss_pct     = args.sl,
        take_profit_pct   = args.tp,
        max_hold_candles  = args.hold,
        position_size_usdt= args.size,
    )

    # 决定回测币种
    if args.symbol:
        symbols = [s.strip().upper() for s in args.symbol.split(",")]
    else:
        print(f"自动筛选24h涨幅>={args.gain}%的币种...")
        gainers = get_top_gainers(min_gain=args.gain)
        symbols = [g["symbol"] for g in gainers[:args.top]]
        if not symbols:
            print("未找到满足条件的币种，退出。")
            return
        print(f"筛选到: {', '.join(symbols)}\n")

    print(f"开始回测 {len(symbols)} 个币种，{args.days} 天数据...\n")
    result = run_backtest(symbols, days=args.days, config=config)

    # 保存
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, default=str)

    # 打印汇总
    s = result["summary"]
    print(f"\n{'='*50}")
    print(f"回测完成")
    print(f"  币种:     {', '.join(symbols)}")
    print(f"  天数:     {args.days}")
    print(f"  总交易:   {s['total_trades']}")
    print(f"  胜率:     {s['win_rate']}%  ({s['wins']}胜 / {s['losses']}负)")
    print(f"  总盈亏:   {s['total_pnl']:+.4f} USDT")
    print(f"  平均盈亏: {s['avg_pnl_pct']:+.2f}%")
    print(f"  结果:     {args.output}")
    print(f"{'='*50}")


if __name__ == "__main__":
    main()
