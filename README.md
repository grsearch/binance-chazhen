# 插针策略 / Spike Strategy Bot

币安现货1分钟K线插针捕捉策略，含虚拟盘测试和回测功能。

## 策略逻辑

```
筛选 → 涨幅 ≥ 50% 且成交量 ≥ 50万USDT 的现货币种
触发 → 每根1分钟K线收盘后，若为下跌阴线（跌幅≥0.5% 且连续下跌）
挂单 → 以该K线最低价为基准，向下挂三档限价买单（-4% / -6% / -9%）
轮换 → 下一根K线结束时撤单，重新评估是否挂新单
出场 → 止损(-3%) | 快速止盈(+5%) | 回弹后阴线 | 超时(10根K线)
```

## 文件结构

```
spike-strategy/
├── strategy.py       # 核心策略逻辑（可复用）
├── backtest.py       # 命令行回测引擎（调用真实币安K线）
├── dashboard.html    # 可视化Dashboard（虚拟盘 + 回测）
└── README.md
```

## 快速开始

### 1. Dashboard（推荐先用）

直接用浏览器打开 `dashboard.html`，无需任何依赖。

- **虚拟盘Tab**：点「扫描」→ 选币种 → 「启动虚拟盘」
- **回测Tab**：输入币种或留空自动筛选 → 「开始回测」（调用真实币安API）
- **配置Tab**：调整策略参数，保存后立即生效

### 2. 命令行回测

```bash
# 依赖：Python 3.10+，无需第三方库
python backtest.py --symbol BTCUSDT --days 3
python backtest.py --days 7 --gain 50 --output result.json

# 完整参数
python backtest.py \
  --symbol BTCUSDT,ETHUSDT \  # 逗号分隔，留空则自动筛选
  --days 3 \                  # 回测天数
  --gain 50 \                 # 最低24h涨幅%
  --top 5 \                   # 自动筛选时取前N名
  --sl 3 \                    # 止损%
  --tp 5 \                    # 止盈%
  --hold 10 \                 # 最大持仓K线数
  --size 100 \                # 每次投入USDT
  --output result.json
```

### 3. 在自己程序中使用策略核心

```python
from strategy import SpikeStrategy, StrategyConfig

cfg      = StrategyConfig(stop_loss_pct=3.0, take_profit_pct=5.0)
strategy = SpikeStrategy(cfg)

# 每根K线收盘后调用
candle = {
    "open": 0.186, "high": 0.190, "low": 0.175,
    "close": 0.178, "volume": 100000,
    "close_time": "2026-04-13 14:00:00",
    "prev_close": 0.185,
}
result = strategy.on_candle_close("BTCUSDT", candle)
print(result)  # {"symbol": ..., "in_trade": ..., "events": [...]}

# 获取统计
print(strategy.get_stats())
print(strategy.get_trades_json())
```

## 挂单参数说明

| 参数 | 默认值 | 说明 |
|------|--------|------|
| order_pct_1/2/3 | 4% / 6% / 9% | 三档挂单距K线最低价的跌幅 |
| order_ratio_1/2/3 | 40% / 35% / 25% | 各档仓位占比（合计100%） |
| min_candle_drop_pct | 0.5% | K线最小跌幅才触发挂单 |
| cooldown_candles | 2 | 出场后冷却K线数 |

## 出场优先级

```
止损 (-3%) > 超时出局 (10根K线) > 快速止盈 (+5%) > 回弹后阴线
```

## 注意事项

- 本项目仅用于学习和研究，不构成投资建议
- 虚拟盘和真实盘之间存在滑点、手续费等差异
- 建议虚拟盘稳定运行至少1周后再考虑实盘
- 实盘需要自行接入币安API签名下单逻辑

## Bug修复记录（v1.1）

1. `should_monitor`：黑名单改为精确symbol匹配，避免`USDT`关键字误杀所有交易对
2. `check_exit`：`candles_since_entry`统一在函数顶部递增，止损分支不再漏计
3. `pnl_usdt`：改为`(exit_price - entry_price) * qty`，避免百分比反推的浮点误差
4. `on_candle_close`：有持仓时提前返回，不走挂单评估分支
5. `build_orders`：价格精度改为8位，兼容低价小币
6. `get_trades_json`：枚举值正确序列化为字符串
7. 回测引擎：支持分页拉取，突破单次1000根K线限制
