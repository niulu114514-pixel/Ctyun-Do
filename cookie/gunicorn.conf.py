#!/usr/bin/env python3
"""
Gunicorn 配置

当前短信验证会话保存在应用内存中。
因此这里使用 1 个 worker + 多线程，避免 /api/login 和 /api/login/verify
被分发到不同进程后取不到同一个 session。
如果后续要扩展成多进程/多机器部署，需要把 session 状态迁移到 Redis 或数据库。
"""

import os

bind = os.getenv("GUNICORN_BIND", "0.0.0.0:5000")
worker_class = "gthread"
workers = int(os.getenv("GUNICORN_WORKERS", "1"))
threads = int(os.getenv("GUNICORN_THREADS", "8"))
timeout = int(os.getenv("GUNICORN_TIMEOUT", "420"))
graceful_timeout = int(os.getenv("GUNICORN_GRACEFUL_TIMEOUT", "30"))
keepalive = int(os.getenv("GUNICORN_KEEPALIVE", "5"))
accesslog = "-"
errorlog = "-"
capture_output = True
loglevel = os.getenv("GUNICORN_LOG_LEVEL", "info")
