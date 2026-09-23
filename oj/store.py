# -*- coding: utf-8 -*-
"""存储层：SQLite 读写。

为什么是 SQLite
---------------
它就在 Python 标准库里（import sqlite3 即可），单文件、零配置、支持标准 SQL。
对一个单机 OJ 来说完全够用，而且表结构设计得和 PostgreSQL / MySQL 通用，
以后要换数据库只需要重写这一个文件。

并发模型
--------
判题 worker 是多线程的，而 sqlite3 的连接对象不能跨线程共享。
这里的做法是：每次操作都开一个新的短连接，用完就关。
对小规模 OJ 来说这个开销可以忽略，换来的是零心智负担的线程安全。

（如果要扛更高的写入量，就该上连接池，或者换成 WAL 模式 + 单写线程。
这是面试时很好的追问点，可以先想清楚。）
"""

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from . import config
from .models import JudgeStatus, Problem

# ---------------------------------------------------------------------------
# 建表语句
#
# 设计要点：
# - submissions 和 testcase_results 是一对多，拆表而不是塞 JSON 字段，
#   这样"统计某道题的通过率""找出最慢的测试点"这类查询可以直接用 SQL 表达
# - 所有 time_ms / memory_kb 都用 INTEGER 存整数，避免浮点误差
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS problems (
    id              TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    time_limit_ms   INTEGER NOT NULL,
    memory_limit_mb INTEGER NOT NULL,
    output_limit_kb INTEGER NOT NULL,
    checker_json    TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS submissions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    problem_id  TEXT NOT NULL,
    language    TEXT NOT NULL,
    source_code TEXT NOT NULL,
    status      TEXT NOT NULL,
    time_ms     INTEGER NOT NULL DEFAULT 0,
    memory_kb   INTEGER,
    message     TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    judged_at   TEXT
);

CREATE INDEX IF NOT EXISTS idx_submissions_problem ON submissions(problem_id, id DESC);

CREATE TABLE IF NOT EXISTS testcase_results (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id INTEGER NOT NULL,
    case_index    INTEGER NOT NULL,
    status        TEXT NOT NULL,
    time_ms       INTEGER NOT NULL DEFAULT 0,
    message       TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_cases_submission ON testcase_results(submission_id, case_index);
"""


def _now() -> str:
    """统一的时间戳格式（本地时间，精确到秒）。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class Store:
    """所有数据库操作的入口。"""

    def __init__(self, db_path: Path = None):
        self.db_path = Path(db_path or config.DB_PATH)
        # 确保父目录存在，否则 sqlite 会因为找不到目录而报错
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    # -- 连接管理 -----------------------------------------------------------

    @contextmanager
    def _conn(self):
        """开一个短连接，正常结束就提交，出异常就回滚。

        timeout=10 表示写锁冲突时最多等 10 秒再报错，
        这对多 worker 同时写库的场景是必要的缓冲。
        """
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        # 让查询结果支持按下标或按列名访问，读起来更清楚
        conn.row_factory = sqlite3.Row
        # 打开外键约束（SQLite 默认是关的，这点很容易踩坑）
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init_schema(self):
        """建表。重复执行是安全的（都用了 IF NOT EXISTS）。"""
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    # -- 题库 ---------------------------------------------------------------

    def upsert_problem(self, p: Problem):
        """写入或更新题目。

        服务启动时会把磁盘上的题库目录全量同步进来，
        所以这里用 UPSERT：题目文件改了，重启服务就能生效。
        """
        import json

        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO problems
                    (id, title, description, time_limit_ms, memory_limit_mb, output_limit_kb, checker_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    title           = excluded.title,
                    description     = excluded.description,
                    time_limit_ms   = excluded.time_limit_ms,
                    memory_limit_mb = excluded.memory_limit_mb,
                    output_limit_kb = excluded.output_limit_kb,
                    checker_json    = excluded.checker_json
                """,
                (
                    p.id,
                    p.title,
                    p.description,
                    p.time_limit_ms,
                    p.memory_limit_mb,
                    p.output_limit_kb,
                    json.dumps(p.checker, ensure_ascii=False),
                ),
            )

    def list_problems(self) -> List[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, title, time_limit_ms, memory_limit_mb FROM problems ORDER BY id"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_problem(self, problem_id: str) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM problems WHERE id = ?", (problem_id,)
            ).fetchone()
        return dict(row) if row else None

    # -- 提交 ---------------------------------------------------------------

    def create_submission(self, problem_id: str, language: str, source_code: str) -> int:
        """落库一条新提交，返回自增 id。

        注意状态初值是 PENDING 而不是 JUDGING ——
        真正的判题由 worker 去做，HTTP 线程只负责入队，立刻返回 id。
        这就是"提交是异步的"：用户拿到 id 后轮询结果。
        """
        with self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO submissions (problem_id, language, source_code, status, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (problem_id, language, source_code, JudgeStatus.PENDING.value, _now()),
            )
            return int(cur.lastrowid)

    def mark_judging(self, submission_id: int):
        with self._conn() as conn:
            conn.execute(
                "UPDATE submissions SET status = ? WHERE id = ?",
                (JudgeStatus.JUDGING.value, submission_id),
            )

    def update_message(self, submission_id: int, message: str):
        """更新进度信息。

        判题过程中 worker 会反复调用它，把"编译中""运行测试点 2/5"写进去，
        前端轮询时就能看到进度，而不是干等一个"判题中"。
        只改 message 不动 status，因为状态转换由 save_result 一次性完成。
        """
        with self._conn() as conn:
            conn.execute(
                "UPDATE submissions SET message = ? WHERE id = ?",
                (message, submission_id),
            )

    def save_result(
        self,
        submission_id: int,
        status: JudgeStatus,
        time_ms: int = 0,
        memory_kb: Optional[int] = None,
        message: str = "",
        cases=None,
    ):
        """把判题结果落库。

        提交主表和测试点明细表一起更新，放在同一个事务里，
        避免出现"主表说 AC 了，明细表里却有 WA 的点"这种不一致。
        """
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE submissions
                   SET status = ?, time_ms = ?, memory_kb = ?, message = ?, judged_at = ?
                 WHERE id = ?
                """,
                (status.value, time_ms, memory_kb, message, _now(), submission_id),
            )
            if cases:
                conn.execute(
                    "DELETE FROM testcase_results WHERE submission_id = ?", (submission_id,)
                )
                conn.executemany(
                    """
                    INSERT INTO testcase_results
                        (submission_id, case_index, status, time_ms, message)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    [
                        (submission_id, c.index, c.status.value, c.time_ms, c.message)
                        for c in cases
                    ],
                )

    def get_submission(self, submission_id: int) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM submissions WHERE id = ?", (submission_id,)
            ).fetchone()
            if not row:
                return None
            data = dict(row)
            cases = conn.execute(
                """
                SELECT case_index, status, time_ms, message
                  FROM testcase_results
                 WHERE submission_id = ?
                 ORDER BY case_index
                """,
                (submission_id,),
            ).fetchall()
        data["cases"] = [dict(c) for c in cases]
        return data

    def list_submissions(self, problem_id: Optional[str] = None, limit: int = 20) -> List[dict]:
        sql = """
            SELECT id, problem_id, language, status, time_ms, memory_kb, message, created_at
              FROM submissions
        """
        params: list = []
        if problem_id:
            sql += " WHERE problem_id = ?"
            params.append(problem_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))

        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def pending_submissions(self) -> List[int]:
        """找出所有还卡在 PENDING / JUDGING 的提交。

        用于服务重启后的恢复：上次没判完的提交不能被永久遗忘。
        """
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id FROM submissions WHERE status IN (?, ?) ORDER BY id",
                (JudgeStatus.PENDING.value, JudgeStatus.JUDGING.value),
            ).fetchall()
        return [int(r["id"]) for r in rows]
