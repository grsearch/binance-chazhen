"""
Flask 后端 API
提供给 dashboard.html 调用的所有接口

启动：
  python server.py

接口列表：
  GET  /api/status          运行状态 + 持仓 + 统计
  GET  /api/trades          历史交易记录
  GET  /api/config          当前配置（secret脱敏）
  POST /api/config          更新并保存配置
  POST /api/start           启动策略
  POST /api/stop            停止策略
  POST /api/reset           重置（仅paper模式）
  POST /api/scan            手动触发扫描
  POST /api/symbol/add      添加监控币种
  POST /api/symbol/remove   移除监控币种
  GET  /                    返回 dashboard.html
"""

import os
import sys
import json
import logging
from pathlib import Path

# ── 确保能 import 同目录模块 ──
sys.path.insert(0, os.path.dirname(__file__))

from engine import get_engine

# ── 尝试导入 Flask ──
try:
    from flask import Flask, request, jsonify, send_from_directory
    from flask_cors import CORS
except ImportError:
    print("缺少依赖，请先运行：pip install flask flask-cors")
    sys.exit(1)

# ── 日志配置 ──
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(
            os.path.join(os.path.dirname(__file__), "data", "server.log"),
            encoding="utf-8"
        ),
    ]
)
logger = logging.getLogger(__name__)

app    = Flask(__name__)
CORS(app)   # 允许跨域（dashboard.html 直接打开时需要）

DASHBOARD_DIR = os.path.dirname(os.path.dirname(__file__))   # 上一级目录


# ═══════════════════════════════════════════════
# 静态文件
# ═══════════════════════════════════════════════

@app.route("/")
def index():
    return send_from_directory(DASHBOARD_DIR, "dashboard.html")


# ═══════════════════════════════════════════════
# 状态 & 数据接口
# ═══════════════════════════════════════════════

@app.route("/api/status")
def api_status():
    try:
        return jsonify(get_engine().get_status())
    except Exception as e:
        logger.exception("status error")
        return jsonify({"error": str(e)}), 500


@app.route("/api/trades")
def api_trades():
    try:
        page  = int(request.args.get("page", 1))
        limit = int(request.args.get("limit", 100))
        all_trades = get_engine().get_trades()
        start = (page - 1) * limit
        return jsonify({
            "trades": all_trades[start: start + limit],
            "total":  len(all_trades),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ═══════════════════════════════════════════════
# 配置接口
# ═══════════════════════════════════════════════

@app.route("/api/config", methods=["GET"])
def api_config_get():
    try:
        return jsonify(get_engine().get_config())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/config", methods=["POST"])
def api_config_post():
    try:
        data = request.get_json(force=True)
        if not data:
            return jsonify({"error": "无效的JSON"}), 400

        # 类型转换
        float_fields = [
            "min_gain_24h","min_volume_usdt","high_price_ratio",
            "order_pct_1","order_pct_2","order_pct_3",
            "order_ratio_1","order_ratio_2","order_ratio_3",
            "stop_loss_pct","take_profit_pct","trailing_red_min_pct",
            "position_size_usdt",
        ]
        int_fields = ["max_hold_candles","cooldown_candles","max_open_positions"]
        for f in float_fields:
            if f in data:
                data[f] = float(data[f])
        for f in int_fields:
            if f in data:
                data[f] = int(data[f])

        result = get_engine().update_config(data)
        return jsonify(result)
    except Exception as e:
        logger.exception("config update error")
        return jsonify({"error": str(e)}), 500


# ═══════════════════════════════════════════════
# 控制接口
# ═══════════════════════════════════════════════

@app.route("/api/start", methods=["POST"])
def api_start():
    try:
        return jsonify(get_engine().start())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/stop", methods=["POST"])
def api_stop():
    try:
        return jsonify(get_engine().stop())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/reset", methods=["POST"])
def api_reset():
    try:
        return jsonify(get_engine().reset())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/scan", methods=["POST"])
def api_scan():
    try:
        return jsonify(get_engine().manual_scan())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ═══════════════════════════════════════════════
# 币种管理
# ═══════════════════════════════════════════════

@app.route("/api/symbol/add", methods=["POST"])
def api_symbol_add():
    try:
        data   = request.get_json(force=True) or {}
        symbol = data.get("symbol", "").strip().upper()
        if not symbol:
            return jsonify({"error": "symbol不能为空"}), 400
        return jsonify(get_engine().add_symbol(symbol))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/symbol/remove", methods=["POST"])
def api_symbol_remove():
    try:
        data   = request.get_json(force=True) or {}
        symbol = data.get("symbol", "").strip().upper()
        return jsonify(get_engine().remove_symbol(symbol))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ═══════════════════════════════════════════════
# 启动
# ═══════════════════════════════════════════════

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8888)
    args = parser.parse_args()

    # 预热引擎（加载持久化数据）
    engine = get_engine()
    logger.info(f"引擎加载完成 模式={engine.cfg.get('mode','paper')} "
                f"监控={len(engine.state.get('symbols',[]))}个币种")

    # 如果上次运行时是启动状态，自动恢复
    if engine.state.get("running"):
        logger.info("检测到上次未停止，自动恢复运行...")
        engine.start()

    logger.info(f"服务启动 http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=False, threaded=True)
