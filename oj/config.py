# -*- coding: utf-8 -*-
"""全局配置。

设计说明
--------
1. 所有可调参数集中在这一个文件里，避免散落在各模块，方便部署时统一调整。
2. 每一项都支持用环境变量覆盖，这样同一份代码在本地和服务器上都能跑。
3. 路径统一从项目根目录推导，不依赖"当前工作目录"，避免从别的目录启动时找不到文件。
"""

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------

# 本文件位于 <项目根>/oj/config.py，所以向上两级就是项目根目录
BASE_DIR = Path(__file__).resolve().parent.parent

# 题库目录：每个题目一个子目录
PROBLEMS_DIR = Path(os.getenv("OJ_PROBLEMS_DIR", str(BASE_DIR / "problems")))

# 用户代码存放目录（把每次提交的源码落盘，方便复现和排查）
SUBMISSIONS_DIR = Path(os.getenv("OJ_SUBMISSIONS_DIR", str(BASE_DIR / "submissions")))

# 评测运行时目录：编译产物、程序输出等临时文件都放这里，可随时清空
RUNTIME_DIR = Path(os.getenv("OJ_RUNTIME_DIR", str(BASE_DIR / "runtime")))

# SQLite 数据库文件路径（SQLite 是 Python 标准库自带的，不需要额外安装）
DB_PATH = Path(os.getenv("OJ_DB_PATH", str(BASE_DIR / "oj.db")))

# 静态资源目录（前端页面）
STATIC_DIR = Path(__file__).resolve().parent / "static"


# ---------------------------------------------------------------------------
# 服务
# ---------------------------------------------------------------------------

HOST = os.getenv("OJ_HOST", "127.0.0.1")
PORT = int(os.getenv("OJ_PORT", "8000"))

# 判题 worker 数量。这是"并发判题数"的上限：
# worker 越多吞吐越高，但同时占用的 CPU 也越多，需要按机器核数调整。
WORKER_COUNT = int(os.getenv("OJ_WORKERS", "4"))


# ---------------------------------------------------------------------------
# 资源限制（题目可以在 problem.json 里单独覆盖）
# ---------------------------------------------------------------------------

DEFAULT_TIME_LIMIT_MS = 1000     # 时间限制：1000 毫秒
DEFAULT_MEMORY_LIMIT_MB = 256    # 内存限制：256 MB
DEFAULT_OUTPUT_LIMIT_KB = 1024   # 输出限制：1 MB，超出即判定为 OLE

# 编译超时单独设置：编译通常比运行慢，但也不能无限等
COMPILE_TIMEOUT_SEC = 15

# 是否保留判题现场文件。
#
# 默认关闭：判完就把运行时目录清掉，避免磁盘被一点点吃满。
# 打开之后（设置环境变量 OJ_KEEP_RUNTIME=1），每个提交的编译产物、用户输出
# 都会留在 runtime/ 里，排查"我明明是对的为什么判错"时非常有用。
# 这是一个真实 OJ 也会提供的调试开关。
KEEP_RUNTIME_FILES = os.getenv("OJ_KEEP_RUNTIME", "0") == "1"


# ---------------------------------------------------------------------------
# 沙箱
# ---------------------------------------------------------------------------

# 沙箱后端：
#   local  —— 直接用本机子进程，跨平台可用（Windows 友好），是简化版的默认选择
#   docker —— 用容器做隔离，安全等级高很多，线上部署应该用这个
SANDBOX_BACKEND = os.getenv("OJ_SANDBOX", "local")

# DockerSandbox 使用的镜像。这个镜像里需要有 g++ / python3。
DOCKER_IMAGE = os.getenv("OJ_DOCKER_IMAGE", "gcc:13")
