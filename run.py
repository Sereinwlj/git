# -*- coding: utf-8 -*-
"""启动入口。

用法：
    python run.py                     默认 127.0.0.1:8000
    python run.py --port 9000         换端口
    python run.py --workers 8         调大并发判题数

启动过程做了四件事：
    1. 建数据库表（幂等，重复启动不会出错）
    2. 从 problems/ 目录同步题库
    3. 捞回上次没判完的提交，拉起 worker 池
    4. 启动 HTTP 服务
"""

import argparse
import sys
from pathlib import Path

# 把项目根目录加进模块搜索路径。
# 这样无论从哪个目录执行 `python run.py`，都能正确 import 到 oj 包。
sys.path.insert(0, str(Path(__file__).resolve().parent))

from oj import config  # noqa: E402
from oj.server import run_server  # noqa: E402
from oj.service import JudgeService  # noqa: E402
from oj.store import Store  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="迷你在线判题机")
    parser.add_argument("--host", default=config.HOST, help="监听地址")
    parser.add_argument("--port", type=int, default=config.PORT, help="监听端口")
    parser.add_argument("--workers", type=int, default=config.WORKER_COUNT, help="并发判题 worker 数")
    parser.add_argument("--sandbox", default=config.SANDBOX_BACKEND, choices=["local", "docker"],
                        help="沙箱后端：local 跨平台可用，docker 隔离更强")
    args = parser.parse_args()

    # 确保几个运行时目录存在，避免第一次启动时因为目录缺失而失败
    for d in (config.RUNTIME_DIR, config.SUBMISSIONS_DIR):
        d.mkdir(parents=True, exist_ok=True)

    store = Store()
    store.init_schema()

    service = JudgeService(store, worker_count=args.workers)
    # 用命令行参数覆盖沙箱选择
    from oj.sandbox import get_sandbox
    service.judge.sandbox = get_sandbox(args.sandbox)

    service.start()
    run_server(store, service, args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
