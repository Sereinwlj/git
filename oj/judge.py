# -*- coding: utf-8 -*-
"""判题核心。

一次判题的完整流程
------------------
    1. 把用户源码写进独立的运行时目录（每次提交一个目录，互不干扰）
    2. 编译型语言先编译 —— 编译失败直接判 CE，不浪费时间去跑测试点
    3. 逐测试点执行：喂输入 -> 拿输出 -> 交给 checker 判对错
    4. 遇到第一个不通过的点就停止（这是常规 OJ 的行为，也省时间）
    5. 汇总：总耗时取所有测试点的最大值，总状态取第一个失败点的状态

"逐点停止"是个值得想清楚的设计
------------------------------
好处是失败得早、省资源；坏处是用户只能看到一个错误点，得改一轮跑一轮。
真实 OJ（比如 Codeforces）会给"第 7 个点 WA"这样的信息，
再进一步就是 ICPC 现场赛那种全部测完再报结果，但那需要更多资源。
这里选了最省资源的一种，并且明确把"在第几个点挂的"告诉用户。
"""

import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from . import config
from .models import CaseResult, JudgeResult, JudgeStatus, Problem, excerpt
from .sandbox import BaseSandbox, RunResult, get_sandbox


# ---------------------------------------------------------------------------
# 语言定义
#
# 新增一门语言只需要在这里加一条，判题流程完全不用动 ——
# 编译命令和运行命令被抽象成模板，用占位符替换路径。
# ---------------------------------------------------------------------------


@dataclass
class Language:
    id: str
    display: str
    source_name: str
    compile_cmd: Optional[List[str]]  # None 表示解释型语言，不需要编译
    run_cmd: List[str]


LANGUAGES = {
    "cpp": Language(
        id="cpp",
        display="C++17",
        source_name="main.cpp",
        compile_cmd=["g++", "-O2", "-std=c++17", "-pipe", "-Wall", "-o", "{exe}", "{src}"],
        run_cmd=["{exe}"],
    ),
    "python": Language(
        id="python",
        display="Python 3",
        source_name="main.py",
        compile_cmd=None,
        run_cmd=["{python}", "-X", "utf8", "{src}"],
    ),
}


def _exe_name() -> str:
    """可执行文件的名字。Windows 上必须带 .exe 后缀，否则 subprocess 找不到。"""
    return "main.exe" if os.name == "nt" else "main"


# 用户输出落盘时的文件名。checker 和 SPJ 脚本都按这个约定去读用户输出。
USER_OUTPUT_NAME = "_user_out.txt"


# ---------------------------------------------------------------------------
# 输出比对器（checker）
#
# 判"对错"这件事并不总是字符串相等。三种典型场景：
#   exact  —— 绝大多数题，逐行比较（忽略行尾空格）
#   float  —— 浮点答案题，允许 eps 误差
#   script —— Special Judge，多解题 / 构造题，必须用一段程序来判
#
# 用外部脚本做 SPJ 是这个项目里最能体现竞赛背景的一块。
# ---------------------------------------------------------------------------


class BaseChecker:
    """比对器接口。

    注意参数是**文件路径**而不是字符串：因为 SPJ 需要把用户输出当作文件
    交给外部脚本去读，统一成文件能少一次拷贝。
    """

    name = "base"

    def check(
        self,
        input_path: Path,
        user_out_path: Path,
        answer_path: Path,
        workdir: Path,
    ) -> Tuple[bool, str]:
        raise NotImplementedError


def _normalize(text: str) -> List[str]:
    """标准化文本，再做比较。

    三步处理：统一换行符、去掉每行行尾空白、丢掉末尾的空行。
    这是 OJ 的行业惯例 —— 用户多打一个空格不该判错，但行内空格必须严格。
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    while lines and lines[-1] == "":
        lines.pop()
    return lines


class ExactChecker(BaseChecker):
    """逐行精确比较（忽略行尾空白与末尾空行）。"""

    name = "exact"

    def check(self, input_path, user_out_path, answer_path, workdir):
        user_text = _read_text(user_out_path)
        answer_text = _read_text(answer_path)

        user_lines = _normalize(user_text)
        answer_lines = _normalize(answer_text)

        if user_lines == answer_lines:
            return True, "输出与标准答案一致"

        # 找出第一个不同的行，给用户一个能直接定位的提示
        limit = max(len(user_lines), len(answer_lines))
        for i in range(limit):
            u = user_lines[i] if i < len(user_lines) else "<没有更多输出>"
            a = answer_lines[i] if i < len(answer_lines) else "<缺少这一行>"
            if u != a:
                return False, f"第 {i + 1} 行不同：你的输出 {u!r}，期望 {a!r}"
        return False, "输出不一致"


class FloatChecker(BaseChecker):
    """浮点比较：逐 token 按相对/绝对误差判定。"""

    name = "float"

    def __init__(self, eps: float = 1e-6):
        self.eps = float(eps)

    def check(self, input_path, user_out_path, answer_path, workdir):
        user_tokens = _read_text(user_out_path).split()
        answer_tokens = _read_text(answer_path).split()

        if len(user_tokens) != len(answer_tokens):
            return False, f"输出 token 数量不同：得到 {len(user_tokens)} 个，期望 {len(answer_tokens)} 个"

        for i, (u, a) in enumerate(zip(user_tokens, answer_tokens)):
            try:
                uf = float(u)
                af = float(a)
            except ValueError:
                return False, f"第 {i + 1} 个输出 {u!r} 不是合法数字"

            # 相对误差 + 绝对误差取较大者，这样大数小数都能正确处理
            if abs(uf - af) > self.eps * max(1.0, abs(af)):
                return False, f"第 {i + 1} 个数字误差过大：得到 {uf}，期望 {af}（允许误差 {self.eps}）"

        return True, f"全部 {len(user_tokens)} 个数字在 {self.eps} 误差内一致"


class ScriptChecker(BaseChecker):
    """Special Judge：调用外部脚本判定。

    接口约定（非常关键，这是 SPJ 的"协议"）：
        python checker.py <输入文件> <用户输出文件> <标准答案文件>

        退出码 0  -> 通过
        退出码非0 -> 不通过
        stdout/stderr 的前若干字符会作为失败原因展示给用户

    有了这个机制，"答案不唯一"的题就能判了。
    比如"输出最大子段和以及对应的一段区间"——区间可能有很多个合法解，
    用字符串比较必然误判，必须让一段程序去验证。
    """

    name = "script"

    def __init__(self, script_path: Path):
        self.script_path = Path(script_path)

    def check(self, input_path, user_out_path, answer_path, workdir):
        if not self.script_path.exists():
            return False, f"SPJ 脚本不存在：{self.script_path}"

        cmd = [
            sys.executable,
            str(self.script_path),
            str(input_path),
            str(user_out_path),
            str(answer_path),
        ]
        try:
            proc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
                cwd=str(workdir),
            )
        except subprocess.TimeoutExpired:
            return False, "SPJ 脚本执行超时"

        message = (proc.stdout or proc.stderr or b"").decode("utf-8", errors="replace").strip()
        return proc.returncode == 0, message or ("SPJ 通过" if proc.returncode == 0 else "SPJ 判定不通过")


def build_checker(conf: dict, problem_dir: Optional[Path]) -> BaseChecker:
    """按题目配置创建比对器。

    配置长这样（写在 problem.json 里）：
        {"type": "exact"}
        {"type": "float", "eps": 1e-6}
        {"type": "script", "path": "checker.py"}
    """
    conf = conf or {}
    kind = conf.get("type", "exact")

    if kind == "float":
        return FloatChecker(conf.get("eps", 1e-6))

    if kind == "script":
        script = conf.get("path", "checker.py")
        # 相对路径按题目目录解析，这样题库可以整个目录搬走
        script_path = Path(script)
        if not script_path.is_absolute() and problem_dir:
            script_path = Path(problem_dir) / script_path
        return ScriptChecker(script_path)

    return ExactChecker()


# ---------------------------------------------------------------------------
# 判题器
# ---------------------------------------------------------------------------


class Judge:
    """判题器。线程安全：每次 judge() 都在自己的目录里干活，不共享状态。"""

    def __init__(self, sandbox: Optional[BaseSandbox] = None):
        self.sandbox = sandbox or get_sandbox()

    def judge(
        self,
        submission_id: int,
        problem: Problem,
        language_id: str,
        source_code: str,
        on_progress: Optional[Callable[[str], None]] = None,
    ) -> JudgeResult:
        """判一次提交，返回结果。

        on_progress 是个可选回调，worker 用它把"正在编译""跑到第几个点"写回数据库，
        这样前端就能看到进度，而不是盯着"判题中"干等。
        """
        language = LANGUAGES.get(language_id)
        if language is None:
            return JudgeResult(status=JudgeStatus.SYSTEM_ERROR, message=f"不支持的语言：{language_id}")

        problem_dir = Path(problem.dir) if problem.dir else None

        # 1. 每次提交一个独立目录。用 submission_id 命名，出问题可以直接去翻现场。
        workdir = config.RUNTIME_DIR / f"submission_{submission_id}"
        # 目录可能因为上一次判题异常退出而残留，先清干净，保证判题环境是全新的
        if workdir.exists():
            _safe_rmtree(workdir)
        workdir.mkdir(parents=True, exist_ok=True)

        try:
            # 2. 写源码
            src_path = workdir / language.source_name
            src_path.write_text(source_code, encoding="utf-8")

            # 3. 编译
            if language.compile_cmd is not None:
                if on_progress:
                    on_progress("编译中")
                ok, compile_output = self._compile(language, workdir)
                if not ok:
                    return JudgeResult(
                        status=JudgeStatus.COMPILE_ERROR,
                        message=excerpt(compile_output, 1500),
                    )

            # 4. 收集测试点
            cases = _collect_testcases(problem_dir)
            if not cases:
                return JudgeResult(
                    status=JudgeStatus.SYSTEM_ERROR,
                    message=f"题目 {problem.id} 没有找到任何测试点（需要 *.in 与同名 *.out 配对）",
                )

            checker = build_checker(problem.checker, problem_dir)

            # 5. 逐点判题
            results: List[CaseResult] = []
            max_time = 0
            max_memory: Optional[int] = None
            overall = JudgeStatus.ACCEPTED

            for idx, (in_path, ans_path) in enumerate(cases, start=1):
                if on_progress:
                    on_progress(f"运行测试点 {idx}/{len(cases)}")

                case = self._run_case(
                    argv=self._build_run_cmd(language, workdir),
                    workdir=workdir,
                    in_path=in_path,
                    ans_path=ans_path,
                    checker=checker,
                    problem=problem,
                    index=idx,
                )
                results.append(case)

                max_time = max(max_time, case.time_ms)
                if case.memory_kb:
                    max_memory = max(max_memory or 0, case.memory_kb)

                if case.status != JudgeStatus.ACCEPTED:
                    # 第一个失败点就是整份提交的结果，后面的点不再跑
                    overall = case.status
                    break

            accepted = sum(1 for r in results if r.status == JudgeStatus.ACCEPTED)
            if overall == JudgeStatus.ACCEPTED:
                message = f"全部 {len(cases)} 个测试点通过"
            else:
                message = f"在第 {len(results)} 个测试点失败（已通过 {accepted} 个）：{results[-1].message}"

            return JudgeResult(
                status=overall,
                time_ms=max_time,
                memory_kb=max_memory,
                message=message,
                cases=results,
            )

        except Exception as exc:  # noqa: BLE001
            # 兜底：判题机自己出问题，不能把整条队列带崩，如实报告成 SystemError
            return JudgeResult(
                status=JudgeStatus.SYSTEM_ERROR,
                message=f"评测机内部错误：{type(exc).__name__}: {exc}",
            )
        finally:
            # 清理运行时目录：编译产物和用户的输出都会占磁盘，
            # 不清理的话跑几千次提交就会把盘吃满。
            #
            # 但这一步必须是"尽力而为"的：
            # 清理发生在判题结束之后，此时结果已经算出来了，
            # 绝不能因为删不掉一个临时文件就把结果丢掉、或者把 worker 线程搞死。
            # 所以统一走 _safe_rmtree（内部吞掉所有异常）。
            if not config.KEEP_RUNTIME_FILES:
                _safe_rmtree(workdir)

    # -- 编译 ---------------------------------------------------------------

    def _compile(self, language: Language, workdir: Path) -> Tuple[bool, str]:
        """编译用户代码。

        注意：编译器本身**不放进沙箱**。
        原因是编译器需要读写自己的头文件和临时目录，塞进沙箱会非常痛苦，
        而且编译器是可信工具、不执行用户代码（虽然模板实例化会消耗资源）。
        真实 OJ 也会给编译单独的资源配额，这里用超时来兜底。
        """
        cmd = _expand(language.compile_cmd, workdir, workdir / language.source_name)
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(workdir),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=config.COMPILE_TIMEOUT_SEC,
            )
        except FileNotFoundError:
            return False, f"找不到编译器：{cmd[0]}。请确认它已安装并在 PATH 里。"
        except subprocess.TimeoutExpired:
            return False, f"编译超时（超过 {config.COMPILE_TIMEOUT_SEC} 秒）"

        output = (proc.stdout or b"").decode("utf-8", errors="replace") + \
                 (proc.stderr or b"").decode("utf-8", errors="replace")

        return proc.returncode == 0, output

    # -- 单个测试点 ---------------------------------------------------------

    def _run_case(
        self,
        argv: List[str],
        workdir: Path,
        in_path: Path,
        ans_path: Path,
        checker: BaseChecker,
        problem: Problem,
        index: int,
    ) -> CaseResult:
        """跑一个测试点并给出状态。"""
        run: RunResult = self.sandbox.run(
            argv=argv,
            stdin_path=in_path,
            workdir=workdir,
            time_limit_ms=problem.time_limit_ms,
            memory_limit_mb=problem.memory_limit_mb,
            output_limit_kb=problem.output_limit_kb,
        )

        # 把用户输出落到文件里再交给 checker。
        # 为什么不让 checker 直接收字符串？因为 SPJ 是一个独立的子进程，
        # 它需要的是一个能打开的文件路径，统一用文件可以少一次序列化，
        # 也顺手留下了现场证据（判错了可以去翻这个文件）。
        user_out_path = workdir / USER_OUTPUT_NAME
        try:
            user_out_path.write_text(run.stdout, encoding="utf-8")
        except OSError:
            pass

        status, message = self._classify(run, checker, in_path, ans_path, user_out_path, problem)

        case = CaseResult(
            index=index,
            status=status,
            time_ms=run.time_ms,
            memory_kb=run.memory_kb,
            message=message,
        )

        # 只有失败的点才需要留下现场信息（用户要拿它去调 bug）
        if status != JudgeStatus.ACCEPTED:
            case.input_excerpt = excerpt(_read_text(in_path), 300)
            case.user_output_excerpt = excerpt(run.stdout, 300)
            case.expected_output_excerpt = excerpt(_read_text(ans_path), 300)

        return case

    @staticmethod
    def _classify(
        run: RunResult,
        checker: BaseChecker,
        in_path: Path,
        ans_path: Path,
        user_out_path: Path,
        problem: Problem,
    ) -> Tuple[JudgeStatus, str]:
        """把"一次运行的结果"翻译成"一个判题状态"。

        判断顺序是有讲究的，从最确定的原因往下排：
            输出超限 > 超时 > 内存超限 > 运行错误 > 输出比对
        为什么把 OLE 放在最前面？因为程序输出超限时通常也会被我们杀掉，
        退出码看起来像 RE，但真正的原因显然是输出。
        """
        if run.output_exceeded:
            return (
                JudgeStatus.OUTPUT_LIMIT_EXCEEDED,
                f"输出超过限制（{problem.output_limit_kb} KB），已强制终止",
            )

        if run.timed_out:
            return (
                JudgeStatus.TIME_LIMIT_EXCEEDED,
                f"运行超过时间限制（{problem.time_limit_ms} ms）",
            )

        # 内存超限的判定是个启发式。
        # 原因：进程被 ulimit/cgroup 挡住时，表现出来是 malloc 失败 ->
        # 抛 bad_alloc / MemoryError 或者直接收到信号，退出码并不统一。
        # 所以先看错误信息里有没有内存相关的关键字，再看是不是被信号打断。
        memory_hint = ("MemoryError", "bad_alloc", "Cannot allocate memory", "OutOfMemory", "memory limit")
        if any(h in run.stderr for h in memory_hint):
            return JudgeStatus.MEMORY_LIMIT_EXCEEDED, "进程申请内存失败，疑似超出内存限制"

        if run.exit_code < 0 and problem.memory_limit_mb > 0 and run.exit_code in (-6, -11):
            # -6 = SIGABRT，-11 = SIGSEGV，都是内存受限时常见的表现
            return (
                JudgeStatus.MEMORY_LIMIT_EXCEEDED,
                f"进程被信号 {-run.exit_code} 终止，疑似超出内存限制（{problem.memory_limit_mb} MB）",
            )

        if run.exit_code != 0:
            detail = run.stderr.strip() or "（程序没有输出错误信息）"
            return JudgeStatus.RUNTIME_ERROR, f"程序异常退出（退出码 {run.exit_code}）：{excerpt(detail, 200)}"

        # 到这里程序是正常结束的，交给 checker 判断输出对不对
        passed, why = checker.check(in_path, user_out_path, ans_path, user_out_path.parent)
        if passed:
            return JudgeStatus.ACCEPTED, why
        return JudgeStatus.WRONG_ANSWER, why

    # -- 运行命令 -----------------------------------------------------------

    @staticmethod
    def _build_run_cmd(language: Language, workdir: Path) -> List[str]:
        return _expand(language.run_cmd, workdir, workdir / language.source_name)


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


def _expand(template: Optional[List[str]], workdir: Path, src: Path) -> List[str]:
    """把命令模板里的占位符换成真实路径。

    支持三个占位符：
        {exe}    编译产物的绝对路径
        {src}    源码文件绝对路径
        {python} 当前解释器路径（保证用同一个 Python 跑用户脚本）
    """
    if template is None:
        return []
    exe_path = workdir / _exe_name()
    return [
        part.replace("{exe}", str(exe_path))
            .replace("{src}", str(src))
            .replace("{python}", sys.executable)
        for part in template
    ]


def _collect_testcases(problem_dir: Optional[Path]) -> List[Tuple[Path, Path]]:
    """找出所有配好对的测试点。

    规则：data 目录下每个 N.in 必须有对应的 N.out。
    按数字大小排序而不是按字符串排序 —— 否则 10 会排在 2 前面，
    报错时"第几个点"的信息就乱了。
    """
    if problem_dir is None:
        return []

    data_dir = Path(problem_dir) / "data"
    if not data_dir.is_dir():
        return []

    def sort_key(p: Path):
        stem = p.stem
        return (0, int(stem)) if stem.isdigit() else (1, stem)

    pairs: List[Tuple[Path, Path]] = []
    for in_path in sorted(data_dir.glob("*.in"), key=sort_key):
        ans_path = in_path.with_suffix(".out")
        if ans_path.exists():
            pairs.append((in_path, ans_path))
    return pairs


def _read_text(path: Path, cap: int = 1024 * 1024) -> str:
    """读文本文件，容错解码。"""
    try:
        with open(path, "rb") as f:
            return f.read(cap).decode("utf-8", errors="replace")
    except OSError:
        return ""


def _safe_rmtree(path: Path):
    """删除目录，永远不抛异常。

    判题流程里的每一次删除都走这里。理由见 Judge.judge() 的 finally 注释：
    收尾动作失败不该影响已经算出来的判题结果，更不该拖垮 worker 线程。
    """
    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# 题库加载
# ---------------------------------------------------------------------------


def load_problems(problems_dir: Path = None) -> List[Problem]:
    """从磁盘扫描题库目录。

    目录结构约定：
        problems/
        └── 1000-a-plus-b/
            ├── problem.json    题目元信息
            ├── checker.py      可选的 SPJ 脚本
            └── data/
                ├── 1.in
                └── 1.out

    把题目做成"文件即数据"而不是硬编码在数据库里，好处是：
    加题只需要新建一个目录，题库可以用 git 管理、可以整包分享。
    """
    problems_dir = Path(problems_dir or config.PROBLEMS_DIR)
    if not problems_dir.is_dir():
        return []

    result: List[Problem] = []
    for meta_path in sorted(problems_dir.glob("*/problem.json")):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] 跳过无法解析的题目配置 {meta_path}: {exc}")
            continue

        result.append(
            Problem(
                id=str(meta.get("id") or meta_path.parent.name),
                title=meta.get("title", meta_path.parent.name),
                description=meta.get("description", ""),
                time_limit_ms=int(meta.get("time_limit_ms", config.DEFAULT_TIME_LIMIT_MS)),
                memory_limit_mb=int(meta.get("memory_limit_mb", config.DEFAULT_MEMORY_LIMIT_MB)),
                output_limit_kb=int(meta.get("output_limit_kb", config.DEFAULT_OUTPUT_LIMIT_KB)),
                dir=str(meta_path.parent),
                checker=meta.get("checker", {"type": "exact"}),
            )
        )
    return result
