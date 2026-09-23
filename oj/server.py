# -*- coding: utf-8 -*-
"""HTTP 接口层。

用 Python 标准库的 http.server，不引入任何 Web 框架 ——
对这个规模的服务来说，一个路由分发函数就够了，
省下来的时间花在判题逻辑上更划算。

接口一览
--------
    GET  /                       前端页面
    GET  /api/languages          支持的提交语言
    GET  /api/problems           题目列表
    GET  /api/problems/{id}      题目详情
    POST /api/submissions        提交代码           -> {"id": 12}
    GET  /api/submissions/{id}   查询单次判题结果
    GET  /api/submissions        提交记录列表

前端拿到的整体协议是「提交 -> 拿 id -> 轮询结果」，
这也是所有 OJ 的通用做法。
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import config
from .judge import LANGUAGES
from .models import JudgeStatus
from .service import JudgeService
from .store import Store


class OjHandler(BaseHTTPRequestHandler):
    # 这些是挂在类上的，由 run_server() 在启动时注入
    store: Store = None
    service: JudgeService = None
    server_version = "MiniOJ/0.1"

    # -- 基础响应工具 -------------------------------------------------------

    def _send_json(self, payload, status: int = 200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # 提交接口不需要缓存，否则前端可能拿到旧的判题结果
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path, content_type: str):
        try:
            body = path.read_bytes()
        except OSError:
            self._send_json({"error": "资源不存在"}, 404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:  # noqa: BLE001
            return {}
        return data if isinstance(data, dict) else {}

    def log_message(self, fmt, *args):
        """默认实现会把每个请求都打到 stderr，太吵。
        这里只保留一行精简日志，方便观察判题请求。"""
        print(f"[http] {self.address_string()} {fmt % args}")

    # -- 路由 ---------------------------------------------------------------

    def do_GET(self):  # noqa: N802  (标准库要求的方法名)
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)

        if path == "/":
            return self._send_file(config.STATIC_DIR / "index.html", "text/html; charset=utf-8")

        if path == "/api/languages":
            return self._send_json([
                {"id": lang.id, "display": lang.display} for lang in LANGUAGES.values()
            ])

        if path == "/api/problems":
            return self._send_json(self.store.list_problems())

        if path.startswith("/api/problems/"):
            pid = path[len("/api/problems/"):]
            problem = self.store.get_problem(pid)
            if problem is None:
                return self._send_json({"error": f"题目不存在：{pid}"}, 404)
            return self._send_json(_problem_payload(problem))

        if path == "/api/submissions":
            problem_id = (query.get("problem_id") or [None])[0]
            limit = int((query.get("limit") or ["20"])[0])
            # 限制一下上限，防止有人用 limit=999999 把服务拖死
            limit = max(1, min(limit, 100))
            return self._send_json(self.store.list_submissions(problem_id, limit))

        if path.startswith("/api/submissions/"):
            try:
                sid = int(path[len("/api/submissions/"):])
            except ValueError:
                return self._send_json({"error": "提交编号必须是整数"}, 400)
            submission = self.store.get_submission(sid)
            if submission is None:
                return self._send_json({"error": f"提交不存在：{sid}"}, 404)
            return self._send_json(_submission_payload(submission))

        return self._send_json({"error": "接口不存在"}, 404)

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path.rstrip("/") or "/"

        if path == "/api/submissions":
            return self._handle_submit()

        return self._send_json({"error": "接口不存在"}, 404)

    # -- 提交处理 -----------------------------------------------------------

    def _handle_submit(self):
        body = self._read_json_body()

        problem_id = str(body.get("problem_id") or "").strip()
        language = str(body.get("language") or "").strip()
        source_code = body.get("source_code") or ""

        # 参数校验：宁可在这里挡掉，也不要让脏数据进队列
        if not problem_id:
            return self._send_json({"error": "缺少 problem_id"}, 400)
        if language not in LANGUAGES:
            return self._send_json({"error": f"不支持的语言：{language}"}, 400)
        if not source_code.strip():
            return self._send_json({"error": "代码不能为空"}, 400)

        # 限制代码长度。没有这个上限，一个几十 MB 的提交就能把内存和数据库撑爆。
        if len(source_code) > 64 * 1024:
            return self._send_json({"error": "代码超过 64 KB 上限"}, 400)

        try:
            submission_id = self.service.submit(problem_id, language, source_code)
        except ValueError as exc:
            return self._send_json({"error": str(exc)}, 404)

        # 202 Accepted 才是语义正确的返回码：请求已接受，但处理还没完成
        return self._send_json({"id": submission_id, "status": JudgeStatus.PENDING.value}, 202)


# ---------------------------------------------------------------------------
# 响应体组装
# ---------------------------------------------------------------------------


def _problem_payload(row: dict) -> dict:
    return {
        "id": row["id"],
        "title": row["title"],
        "description": row["description"],
        "time_limit_ms": row["time_limit_ms"],
        "memory_limit_mb": row["memory_limit_mb"],
    }


def _submission_payload(row: dict) -> dict:
    """把数据库行翻译成前端友好的结构。

    这里做了一件小事但体验提升很大：把 JudgeStatus 的英文枚举翻译成中文标签。
    让前端去维护一份状态到中文的映射表是很脆的 ——
    后端加一个状态、前端忘了同步，界面就会漏显示。
    所以状态的"含义"应该由后端负责解释。
    """
    try:
        status = JudgeStatus(row["status"])
        status_label = status.label
        is_final = status.is_final
    except ValueError:
        status_label = row["status"]
        is_final = True

    return {
        "id": row["id"],
        "problem_id": row["problem_id"],
        "language": row["language"],
        "status": row["status"],
        "status_label": status_label,
        "is_final": is_final,
        "time_ms": row["time_ms"],
        "memory_kb": row["memory_kb"],
        "message": row["message"],
        "created_at": row["created_at"],
        "judged_at": row["judged_at"],
        "cases": [
            {
                "index": c["case_index"],
                "status": c["status"],
                "status_label": _safe_label(c["status"]),
                "time_ms": c["time_ms"],
                "message": c["message"],
            }
            for c in row.get("cases", [])
        ],
    }


def _safe_label(status: str) -> str:
    try:
        return JudgeStatus(status).label
    except ValueError:
        return status


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------


def run_server(store: Store, service: JudgeService, host: str = None, port: int = None):
    """启动 HTTP 服务（阻塞）。"""
    host = host or config.HOST
    port = int(port or config.PORT)

    # 把依赖挂到 handler 类上。标准库的 handler 是有参构造的，
    # 用类属性传依赖是最省事的做法，也让每个请求都能直接访问。
    OjHandler.store = store
    OjHandler.service = service

    # ThreadingHTTPServer：每个请求一个线程。
    # 这对 OJ 是必须的 —— 前端要不断轮询判题结果，
    # 单线程服务器会被轮询请求堵住，连页面都刷不开。
    httpd = ThreadingHTTPServer((host, port), OjHandler)
    print(f"[oj] Web 界面：http://{host}:{port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[oj] 正在关闭…")
    finally:
        httpd.shutdown()
        httpd.server_close()
