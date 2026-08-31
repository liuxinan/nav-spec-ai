# -*- coding: utf-8 -*-
"""NavSpec AI 入口：python main.py 启动 Web 服务。

退出码语义（沿用参考工程习惯）：0 正常退出（Ctrl+C），3 配置错误。
"""
import sys

from app.config import load_config, ConfigError
from app.pipeline import Pipeline
from app.web import serve


def main() -> int:
    try:
        cfg = load_config()
    except ConfigError as e:
        print("[配置错误] %s" % e, flush=True)
        return 3
    import os
    pipeline = Pipeline(cfg, os.path.dirname(os.path.abspath(__file__)))
    try:
        serve(cfg, pipeline)
    except KeyboardInterrupt:
        print("\n已停止", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
