#!/usr/bin/env python3
"""
健康检查脚本 — 由 systemd timer 每分钟执行
检查Flask服务是否正常响应，不正常则重启
"""
import urllib.request
import subprocess
import sys
import os
from datetime import datetime

PORT    = 8888
SERVICE = "spike-bot"
LOG     = "/opt/spike-bot/live/data/health.log"

def log(msg: str):
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}\n"
    try:
        with open(LOG, "a") as f:
            f.write(line)
        # 只保留最近500行
        with open(LOG, "r") as f:
            lines = f.readlines()
        if len(lines) > 500:
            with open(LOG, "w") as f:
                f.writelines(lines[-500:])
    except Exception:
        pass
    print(line.strip())

def check_service() -> bool:
    try:
        url = f"http://127.0.0.1:{PORT}/api/status"
        req = urllib.request.Request(url, headers={"User-Agent": "healthcheck"})
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status == 200
    except Exception as e:
        log(f"健康检查失败: {e}")
        return False

def restart_service():
    log(f"重启服务 {SERVICE}...")
    try:
        subprocess.run(["systemctl", "restart", SERVICE],
                       check=True, timeout=30)
        log("重启成功")
    except Exception as e:
        log(f"重启失败: {e}")

if __name__ == "__main__":
    if not check_service():
        restart_service()
    else:
        # 正常时不输出（避免日志刷屏）
        pass
