# -*- coding: utf-8 -*-
"""领域模型。

这里定义三样东西：
1. JudgeStatus —— 判题状态机，是整个系统的"词汇表"
2. Problem     —— 题目
3. 一些用于 JSON 序列化的辅助函数

把状态定义成枚举（而不是到处写字符串）有两个好处：
- 拼写错误会在开发阶段就暴露，而不是线上出现一条状态为 "Aceptted" 的记录
- 状态流转关系可以和枚举放在一起，读代码的人一眼能看全所有可能结果
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class JudgeStatus(str, Enum):
    """判题状态机。

    继承 str 是为了能直接被 json.dumps 序列化（否则要额外写转换逻辑）。

    正常流转：

        PENDING ──> JUDGING ──┬──> ACCEPTED
                              ├──> WRONG_ANSWER
                              ├──> TIME_LIMIT_EXCEEDED
                              ├──> MEMORY_LIMIT_EXCEEDED
                              ├──> OUTPUT_LIMIT_EXCEEDED
                              ├──> RUNTIME_ERROR
                              └──> SYSTEM_ERROR

    另外两种不经过 JUDGING：
        COMPILE_ERROR —— 编译阶段就失败了，根本没有进程可跑
        （提交一落库就是 PENDING，worker 取走才变 JUDGING）
    """

    PENDING = "Pending"
    JUDGING = "Judging"

    ACCEPTED = "Accepted"
    WRONG_ANSWER = "WrongAnswer"
    TIME_LIMIT_EXCEEDED = "TimeLimitExceeded"
    MEMORY_LIMIT_EXCEEDED = "MemoryLimitExceeded"
    OUTPUT_LIMIT_EXCEEDED = "OutputLimitExceeded"
    RUNTIME_ERROR = "RuntimeError"
    COMPILE_ERROR = "CompileError"
    SYSTEM_ERROR = "SystemError"

    @property
    def label(self) -> str:
        """给前端看的中文名。"""
        return _STATUS_LABELS[self]

    @property
    def is_final(self) -> bool:
        """是否终态。终态意味着不再变化，前端可以停止轮询。"""
        return self not in (JudgeStatus.PENDING, JudgeStatus.JUDGING)


_STATUS_LABELS = {
    JudgeStatus.PENDING: "等待判题",
    JudgeStatus.JUDGING: "判题中",
    JudgeStatus.ACCEPTED: "通过",
    JudgeStatus.WRONG_ANSWER: "答案错误",
    JudgeStatus.TIME_LIMIT_EXCEEDED: "超出时间限制",
    JudgeStatus.MEMORY_LIMIT_EXCEEDED: "超出内存限制",
    JudgeStatus.OUTPUT_LIMIT_EXCEEDED: "输出超限",
    JudgeStatus.RUNTIME_ERROR: "运行错误",
    JudgeStatus.COMPILE_ERROR: "编译错误",
    JudgeStatus.SYSTEM_ERROR: "评测机异常",
}


@dataclass
class Problem:
    """一道题。

    data 目录里的测试点按文件名配对：
        1.in  <->  1.out
        2.in  <->  2.out
    checker 描述这道题怎么判对错，见 judge.py 里的 Checker 家族。
    """

    id: str
    title: str
    description: str = ""
    time_limit_ms: int = 1000
    memory_limit_mb: int = 256
    output_limit_kb: int = 1024
    # 题目所属目录（运行时才知道，不入库）
    dir: Optional[str] = None
    checker: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        """转成可直接返回给前端的字典。"""
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "time_limit_ms": self.time_limit_ms,
            "memory_limit_mb": self.memory_limit_mb,
        }


@dataclass
class CaseResult:
    """单个测试点的判题结果。"""

    index: int
    status: JudgeStatus
    time_ms: int = 0
    memory_kb: Optional[int] = None
    message: str = ""
    # 不通过的测试点，留下前若干字符的 diff 摘要，方便用户自查
    input_excerpt: str = ""
    user_output_excerpt: str = ""
    expected_output_excerpt: str = ""


@dataclass
class JudgeResult:
    """一次提交的完整判题结果。"""

    status: JudgeStatus
    time_ms: int = 0
    memory_kb: Optional[int] = None
    message: str = ""
    cases: list = field(default_factory=list)


def excerpt(text: str, limit: int = 200) -> str:
    """截断长文本，避免把巨大的输出塞进数据库和接口响应里。"""
    if text is None:
        return ""
    text = text.replace("\r\n", "\n")
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...（共 {len(text)} 字符，已截断）"
