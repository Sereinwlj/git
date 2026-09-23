# -*- coding: utf-8 -*-
"""服务层：提交队列 + worker 池。

为什么判题必须异步
------------------
一次判题要编译 + 跑若干个测试点，很容易超过一秒，慢的时候十几秒。
如果放在 HTTP 请求线程里同步做，会同时踩两个坑：
    1. 请求会超时，用户看到的是一片白屏
    2. HTTP 线程被占满，整个站点失去响应（一个提交就能把服务打死）

所以拆成两个阶段：
    提交（HTTP 线程，快）  ->  入队，立刻返回 submission_id
    判题（worker 线程，慢）->  从队列取任务，判完把结果写进数据库
前端拿到 id 之后轮询结果。这就是"异步判题"的标准形态。

worker 池的意义
---------------
多个 worker 并发判题 = 吞吐量成倍提升。
但 worker 数量不是越多越好：每个 worker 背后是一个真实的用户进程，
worker 开太多会把 CPU 抢空，反而让每个提交都变慢。
经验值是 CPU 核数的 1~2 倍。这里默认 4。
"""

import queue
import threading
import traceback
from pathlib import Path
from typing import Optional

from . import config
from .judge import Judge, load_problems
from .models import JudgeStatus
from .sandbox import get_sandbox
from .store import Store


class JudgeService:
    """把存储、队列、worker、判题器粘在一起。

    对外只暴露两个动作：submit() 提交，以及 start()/stop() 生命周期管理。
    """

    def __init__(self, store: Store, worker_count: int = None, sandbox=None):
        self.store = store
        self.worker_count = worker_count or config.WORKER_COUNT

        # 判题器在多个 worker 之间共享。
        # 之所以安全，是因为 Judge.judge() 不持有任何跨提交的状态：
        # 每次判题都在自己的运行时目录里干活，用完即删。
        self.judge = Judge(sandbox or get_sandbox())

        # 用 Queue 做任务队列。它内部自带锁，是线程安全的，
        # 而且 get() 会阻塞等待，天然实现"有活就干、没活就睡"。
        self.queue: "queue.Queue[int]" = queue.Queue()

        self._workers: list = []
        self._running = False

    # -- 生命周期 -----------------------------------------------------------

    def start(self):
        """同步题库、恢复未完成的提交、拉起 worker。"""
        self._sync_problems()
        self._recover_pending()
        self._running = True

        for i in range(self.worker_count):
            t = threading.Thread(target=self._worker_loop, name=f"judge-worker-{i}", daemon=True)
            t.start()
            self._workers.append(t)

        print(f"[oj] 判题服务已启动：{self.worker_count} 个 worker，沙箱 = {self.judge.sandbox.name}")

    def stop(self):
        self._running = False

    # -- 题库 ---------------------------------------------------------------

    def _sync_problems(self):
        """把磁盘上的题库同步进数据库。

        题目以文件为准（source of truth 在磁盘），数据库只是索引。
        这样加题/改题不需要写 SQL，也不怕数据库丢了题目定义。
        """
        problems = load_problems()
        for p in problems:
            self.store.upsert_problem(p)
        print(f"[oj] 已从 {config.PROBLEMS_DIR} 同步 {len(problems)} 道题目")

    # -- 提交 ---------------------------------------------------------------

    def submit(self, problem_id: str, language: str, source_code: str) -> int:
        """接收一次提交：落库 -> 存源码 -> 入队 -> 返回 id。

        这三步的先后顺序有讲究：先落库再入队。
        因为队列在内存里，进程一重启就没了；而数据库里的记录还在。
        只要记录先落库，哪怕入队后立刻宕机，重启时也能靠 _recover_pending() 捞回来。
        """
        problem = self.store.get_problem(problem_id)
        if problem is None:
            raise ValueError(f"题目不存在：{problem_id}")

        submission_id = self.store.create_submission(problem_id, language, source_code)

        # 源码单独存一份文件。数据库里虽然有源码，但落成文件之后
        # 可以直接复制到本地复现问题，排查用户报的"我明明是对的"会方便很多。
        try:
            config.SUBMISSIONS_DIR.mkdir(parents=True, exist_ok=True)
            safe_lang = "".join(ch for ch in language if ch.isalnum()) or "txt"
            path = self.store_source_path(submission_id, safe_lang)
            path.write_text(source_code, encoding="utf-8")
        except OSError as exc:
            print(f"[warn] 源码落盘失败（不影响判题）：{exc}")

        self.queue.put(submission_id)
        return submission_id

    @staticmethod
    def store_source_path(submission_id: int, language: str) -> Path:
        suffix = {"cpp": ".cpp", "python": ".py"}.get(language, ".txt")
        return config.SUBMISSIONS_DIR / f"{submission_id}{suffix}"

    def _recover_pending(self):
        """服务重启后，把上次没判完的提交重新入队。

        这是"提交先落库、再入队"这个顺序换来的好处：
        数据库是持久的，队列不是，所以只要数据库里能查到，
        就一定能把任务补回来。没有这一步，用户会永远卡在"等待判题"。
        """
        pending = self.store.pending_submissions()
        for sid in pending:
            self.queue.put(sid)
        if pending:
            print(f"[oj] 发现 {len(pending)} 条未完成的提交，已重新入队")

    # -- worker -------------------------------------------------------------

    def _worker_loop(self):
        """单个 worker 的主循环：取任务 -> 判题 -> 写结果。

        这里必须用 try/except 把整个任务包住。
        原因：worker 是一个不会退出的长驻线程，一旦有异常逃出去，这个线程就死了，
        等于永久损失一份判题能力（而且现场很难发现）。宁可把异常记成 SystemError。
        """
        while self._running:
            try:
                submission_id = self.queue.get(timeout=1.0)
            except queue.Empty:
                continue

            try:
                self._handle_one(submission_id)
            except Exception:  # noqa: BLE001
                traceback.print_exc()
                try:
                    self.store.save_result(
                        submission_id,
                        JudgeStatus.SYSTEM_ERROR,
                        message="评测机内部错误，请联系管理员",
                    )
                except Exception:  # noqa: BLE001
                    pass
            finally:
                # 无论成功失败都要 task_done()，否则队列的计数会永久失衡
                self.queue.task_done()

    def _handle_one(self, submission_id: int):
        """判一条提交。"""
        sub = self.store.get_submission(submission_id)
        if sub is None:
            return

        problem_row = self.store.get_problem(sub["problem_id"])
        if problem_row is None:
            self.store.save_result(
                submission_id, JudgeStatus.SYSTEM_ERROR,
                message=f"题目 {sub['problem_id']} 已不存在",
            )
            return

        # 从数据库行还原成一个 Problem 对象。
        # dir 字段不入库（它是运行时信息），这里按题目 id 回到磁盘上找出来。
        from .models import Problem

        problem = Problem(
            id=problem_row["id"],
            title=problem_row["title"],
            description=problem_row["description"],
            time_limit_ms=problem_row["time_limit_ms"],
            memory_limit_mb=problem_row["memory_limit_mb"],
            output_limit_kb=problem_row["output_limit_kb"],
            dir=_problem_dir(problem_row["id"]),
            checker=_parse_json(problem_row["checker_json"]),
        )

        self.store.mark_judging(submission_id)

        result = self.judge.judge(
            submission_id=submission_id,
            problem=problem,
            language_id=sub["language"],
            source_code=sub["source_code"],
            on_progress=lambda msg: self.store.update_message(submission_id, msg),
        )

        self.store.save_result(
            submission_id,
            result.status,
            time_ms=result.time_ms,
            memory_kb=result.memory_kb,
            message=result.message,
            cases=result.cases,
        )


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _problem_dir(problem_id: str) -> str:
    """按题目编号在题库目录里找到真实目录。

    题库目录名不要求等于题目 id（可能叫 1000-a-plus-b），
    所以这里扫一遍目录去匹配 problem.json 里的 id。
    """
    root = Path(config.PROBLEMS_DIR)
    if not root.is_dir():
        return str(root / problem_id)
    for meta in root.glob("*/problem.json"):
        try:
            import json

            data = json.loads(meta.read_text(encoding="utf-8"))
            if str(data.get("id")) == str(problem_id):
                return str(meta.parent)
        except Exception:  # noqa: BLE001
            continue
    return str(root / problem_id)


def _parse_json(text: str) -> dict:
    import json

    try:
        return json.loads(text or "{}")
    except Exception:  # noqa: BLE001
        return {}
