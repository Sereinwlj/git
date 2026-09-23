# -*- coding: utf-8 -*-
"""沙箱层：把一个用户程序在受控环境里跑起来。

这一层的职责
------------
判题核心不关心用户代码是怎么被执行的，它只想要一个答案：
    这次运行花了多久？输出了什么？是不是超时了？退出码是多少？

沙箱层的任务就是**在自己的权限范围内**尽量把用户程序关起来，并且如实报告上面这些信息。
"在自己的权限范围内"这句话很重要 —— 隔离强度直接决定了你能挡下多少种攻击。

两个实现
--------
LocalSandbox
    直接开本机子进程。跨平台可用（Windows 也能跑），是简化版的默认选择。
    能挡住的：死循环（wall clock 超时）、无限输出（输出字节上限 + 及时 kill）
    POSIX 下额外能挡住的：CPU 时间超限、虚拟内存超限、fork 炸弹（进程数上限）
    挡不住的：读写宿主文件系统、网络外联、读别的进程内存 —— 因为它不是容器。

DockerSandbox
    容器隔离。一次性把 pids / cpu / memory / network / 只读根文件系统全部收口。
    代价是需要机器上装了 Docker，而且每次运行有几十毫秒的启动开销。
    **线上部署应该用这个。**

两者实现同一个 run() 契约，所以 JUDGE 层可以完全不改代码就切换沙箱 ——
这就是"面向接口编程"在这里的实际价值。
"""

import math
import os
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional


class SandboxError(Exception):
    """沙箱自身出问题了（不是用户代码的问题）。"""


@dataclass
class RunResult:
    """一次受控运行的完整结果。"""

    exit_code: int = 0
    time_ms: int = 0
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False          # 是否因为超过时间限制被我们杀掉
    output_exceeded: bool = False    # 是否因为输出超过上限被我们杀掉
    memory_kb: Optional[int] = None  # 内存峰值（近似值，见下方注释）
    error: str = ""                  # 沙箱层面的错误信息


# 轮询间隔。取得太小会空转浪费 CPU，太大则超时判定不够精确。
# 5ms 是这两者之间的一个经验平衡点。
POLL_INTERVAL_SEC = 0.005


class BaseSandbox:
    """沙箱接口。

    任何沙箱实现都必须提供这个方法。判题核心只依赖这个接口。
    """

    name = "base"

    def run(
        self,
        argv: List[str],
        stdin_path: Path,
        workdir: Path,
        time_limit_ms: int,
        memory_limit_mb: int,
        output_limit_kb: int,
    ) -> RunResult:
        raise NotImplementedError

    def available(self) -> bool:
        """这个沙箱当前能不能用。用于启动时自检并退化到备选方案。"""
        return True


# ---------------------------------------------------------------------------
# 实现一：本机进程沙箱
# ---------------------------------------------------------------------------


class LocalSandbox(BaseSandbox):
    """用本机子进程执行用户代码。

    隔离手段（按强度从弱到强）：
    1. 工作目录隔离 —— 用户程序在独立的临时目录里运行，不影响别处
    2. 时间限制     —— 父进程轮询 + 超时即杀（wall clock）
    3. 输出限制     —— 边跑边看输出文件大小，超限即杀，防止把磁盘写满
    4. POSIX 下通过 ulimit 追加：CPU 时间、虚拟内存、进程数、core dump

    为什么用 `sh -c "ulimit ...; exec ..."` 而不是 preexec_fn？
    因为 preexec_fn 在**多线程进程中是不安全的**（fork 之后只允许调用异步信号安全的函数，
    而 Python 在子进程里跑任意代码有死锁风险），官方文档明确警告过。
    我们的判题 worker 是多线程的，所以走 shell 内置 ulimit 这条路更稳。
    """

    name = "local"

    def run(
        self,
        argv: List[str],
        stdin_path: Path,
        workdir: Path,
        time_limit_ms: int,
        memory_limit_mb: int,
        output_limit_kb: int,
    ) -> RunResult:
        workdir = Path(workdir)
        token = uuid.uuid4().hex[:12]
        out_path = workdir / f".stdout_{token}"
        err_path = workdir / f".stderr_{token}"

        cap_bytes = max(1, int(output_limit_kb)) * 1024
        launch = self._wrap_argv(argv, time_limit_ms, memory_limit_mb)

        fin = open(stdin_path, "rb")
        fout = open(out_path, "wb")
        ferr = open(err_path, "wb")
        proc = None
        timed_out = False
        output_exceeded = False

        try:
            proc = subprocess.Popen(
                launch,
                stdin=fin,
                stdout=fout,
                stderr=ferr,
                cwd=str(workdir),
            )

            started = time.monotonic()
            # 监控循环：既看时间，也看输出体积。
            # 两条判断都必须在循环里做，因为无论哪一条先触发，都要立刻把进程杀掉。
            while True:
                if proc.poll() is not None:
                    break

                elapsed_ms = int((time.monotonic() - started) * 1000)
                if elapsed_ms > time_limit_ms:
                    timed_out = True
                    self._kill(proc)
                    break

                try:
                    if out_path.stat().st_size > cap_bytes:
                        output_exceeded = True
                        self._kill(proc)
                        break
                except OSError:
                    # 极端情况下文件可能已被清理，忽略即可
                    pass

                time.sleep(POLL_INTERVAL_SEC)

            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                # kill 之后还不退出，说明进程处于不可中断状态，只能放弃
                self._kill(proc)

            elapsed_ms = int((time.monotonic() - started) * 1000)
            exit_code = proc.returncode if proc.returncode is not None else -1

        except FileNotFoundError as exc:
            raise SandboxError(f"无法启动进程：{exc}") from exc
        finally:
            fin.close()
            fout.close()
            ferr.close()

        stdout = _read_capped(out_path, cap_bytes)
        stderr = _read_capped(err_path, 8 * 1024)

        # 运行结束，清掉临时输出文件，避免占满磁盘。
        # 注意这里用的是一个"绝不抛异常"的清理函数：
        # 清理是收尾动作，绝不应该因为它失败而影响判题结果。
        if not _keep_runtime():
            _safe_unlink(out_path)
            _safe_unlink(err_path)

        return RunResult(
            exit_code=exit_code,
            time_ms=elapsed_ms,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            output_exceeded=output_exceeded,
            memory_kb=self._peak_memory_kb(),
        )

    # -- 内部工具 -----------------------------------------------------------

    @staticmethod
    def _wrap_argv(argv: List[str], time_limit_ms: int, memory_limit_mb: int) -> List[str]:
        """在 POSIX 上用 shell 内置的 ulimit 给子进程加上内核级限制。

        Windows 没有 ulimit，直接返回原命令 —— 那边只能靠父进程的时间/输出监控兜底，
        这也是为什么 Windows 上跑 OJ 只能算"能玩"，不能算"能上线"。
        """
        if os.name != "posix":
            return list(argv)

        # CPU 时间给一点余量：真正的超时判定由父进程的 wall clock 负责，
        # ulimit 只是第二道保险，防止用户程序逃过父进程的监控。
        cpu_sec = max(1, int(math.ceil(time_limit_ms / 1000.0)) + 1)

        parts = [
            f"ulimit -t {cpu_sec}",   # CPU 时间上限（秒）
            "ulimit -c 0",            # 不产生 core dump，避免大文件落盘
            "ulimit -u 64",           # 进程/线程数上限，挡 fork 炸弹的第一道防线
        ]
        if memory_limit_mb and memory_limit_mb > 0:
            parts.append(f"ulimit -v {int(memory_limit_mb) * 1024}")  # 虚拟内存上限（KB）

        script = "; ".join(parts) + '; exec "$@"'
        # 注意结尾的 "sh"：它是 $0，argv 里的程序会作为 $@ 传给 exec
        return ["/bin/sh", "-c", script, "sh", *argv]

    @staticmethod
    def _kill(proc: subprocess.Popen):
        """尽力杀掉进程。

        已知短板：Windows 上 Popen.kill() 只能结束当前进程，
        杀不掉它派生出来的子进程。要彻底解决得用 Job Object，
        那是本地沙箱做不到的 —— 这也是应该上 Docker 的另一个理由。
        """
        try:
            proc.kill()
        except Exception:
            pass

    @staticmethod
    def _peak_memory_kb() -> Optional[int]:
        """读取子进程的内存峰值（近似值）。

        重要说明：这里用的是 RUSAGE_CHILDREN.ru_maxrss，它是
        **所有已回收子进程的历史最大值**，只会单调上升、不会回落。
        所以这个数字在并发判题时会偏大，只能当作粗略参考。

        精确做法是读 cgroup v2 的 memory.peak，或者用 os.wait4() 拿单个子进程的 rusage。
        生产环境应该用容器 + cgroup，那是唯一准确的路子。
        """
        if os.name != "posix":
            return None
        try:
            import resource

            rss = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
            # Linux 上单位是 KB，macOS 上单位是字节，这里统一成 KB
            if sys.platform == "darwin":
                rss = rss / 1024
            return int(rss)
        except Exception:
            return None


# ---------------------------------------------------------------------------
# 实现二：容器沙箱
# ---------------------------------------------------------------------------


class DockerSandbox(BaseSandbox):
    """用 Docker 容器执行用户代码。

    容器把本地沙箱挡不住的东西一次性收口：

        --network none                没有网络接口，无法外联、无法探测内网
        --read-only                   根文件系统只读，改不了系统文件
        --tmpfs /tmp                  只给 /tmp 一块可写内存盘，有大小上限
        -v {workdir}:/box:ro          用户代码目录只读挂载，防止自我改写
        --memory / --memory-swap      内存硬限制，且禁止用 swap 绕过
        --cpus 1                      限制 CPU 使用
        --pids-limit 64               进程数上限，直接掐死 fork 炸弹
        -u 65534:65534                以 nobody 身份运行，不是 root
        --security-opt no-new-privileges  禁止提权

    运行开销：每次都要起容器，大约几十毫秒到几百毫秒，
    所以判题队列的吞吐会比本地沙箱低，这是安全换来的代价。
    """

    name = "docker"

    def __init__(self, image: str = "gcc:13"):
        self.image = image

    def available(self) -> bool:
        """检查 docker 是否可用（命令存在 + 守护进程在跑）。"""
        if shutil.which("docker") is None:
            return False
        try:
            proc = subprocess.run(
                ["docker", "info"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
            return proc.returncode == 0
        except Exception:
            return False

    def run(
        self,
        argv: List[str],
        stdin_path: Path,
        workdir: Path,
        time_limit_ms: int,
        memory_limit_mb: int,
        output_limit_kb: int,
    ) -> RunResult:
        workdir = Path(workdir)
        cap_bytes = max(1, int(output_limit_kb)) * 1024

        # 容器内也要限制一下 CPU 时间，作为 docker 参数之外的第二道保险
        cpu_sec = max(1, int(math.ceil(time_limit_ms / 1000.0)) + 1)
        inner = f"ulimit -t {cpu_sec}; ulimit -c 0; exec \"$@\""

        cmd = [
            "docker", "run", "--rm", "-i",
            "--network", "none",
            "--read-only",
            "--tmpfs", "/tmp:rw,size=64m",
            "--memory", f"{int(memory_limit_mb)}m",
            "--memory-swap", f"{int(memory_limit_mb)}m",
            "--cpus", "1",
            "--pids-limit", "64",
            "--security-opt", "no-new-privileges",
            "-u", "65534:65534",
            "-v", f"{workdir}:/box:ro",
            "-w", "/box",
            self.image,
            "/bin/sh", "-c", inner, "sh", *argv,
        ]

        # 用父进程的 wall clock 作为超时基准，额外给 3 秒给容器启动留余量
        hard_timeout = time_limit_ms / 1000.0 + 3.0

        passed_stdin = _read_capped_bytes(stdin_path, 8 * 1024 * 1024)
        started = time.monotonic()
        try:
            proc = subprocess.run(
                cmd,
                input=passed_stdin,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=hard_timeout,
            )
        except subprocess.TimeoutExpired:
            # 超时：docker run 的进程被杀了，容器本身由 --rm 负责清理
            return RunResult(
                exit_code=-1,
                time_ms=int((time.monotonic() - started) * 1000),
                timed_out=True,
                stderr="容器运行超时，已强制终止",
            )

        elapsed_ms = int((time.monotonic() - started) * 1000)
        raw_out = proc.stdout or b""
        raw_err = proc.stderr or b""

        exceeded = len(raw_out) > cap_bytes
        stdout = raw_out[:cap_bytes].decode("utf-8", errors="replace")

        return RunResult(
            exit_code=proc.returncode,
            time_ms=elapsed_ms,
            stdout=stdout,
            stderr=raw_err[:8192].decode("utf-8", errors="replace"),
            output_exceeded=exceeded,
            # 内存这里留空。准确值要去读容器 cgroup v2 的 memory.peak，
            # 但容器已经 --rm 掉了，所以做不了 —— 生产实现应该用
            # `docker run --detach` + 事后读 cgroup 统计，或者用 /usr/bin/time -v 包一层。
            memory_kb=None,
        )


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------


def get_sandbox(backend: str = None) -> BaseSandbox:
    """按配置创建沙箱。

    带自动降级：如果配置了 docker 但机器上不可用（没装 / 守护进程没起），
    不要直接崩掉，而是打印预警后退回本地沙箱 —— 服务可用性优先。
    """
    from . import config

    backend = backend or config.SANDBOX_BACKEND

    if backend == "docker":
        docker = DockerSandbox(config.DOCKER_IMAGE)
        if docker.available():
            return docker
        print("[warn] 配置了 docker 沙箱但 Docker 不可用，已自动降级为 local 沙箱")
        return LocalSandbox()

    return LocalSandbox()


# ---------------------------------------------------------------------------
# 文件读取工具
# ---------------------------------------------------------------------------


def _read_capped_bytes(path: Path, cap: int) -> bytes:
    """最多读 cap 个字节，防止把巨大的输出读进内存。"""
    try:
        with open(path, "rb") as f:
            return f.read(cap)
    except OSError:
        return b""


def _read_capped(path: Path, cap: int) -> str:
    """同上，但返回解码后的字符串。用 replace 策略，避免非法字节直接抛异常。"""
    return _read_capped_bytes(path, cap).decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# 收尾动作（全部做成"永远不会抛异常"）
#
# 为什么这么谨慎
# --------------
# 判题流程里，清理只是收尾；但如果清理抛出的异常穿透出去，
# 整个 worker 线程就可能挂掉 —— 表现是用户的提交永远停在"判题中"。
# 一个洁癖式的删除动作搞死整条判题队列，是很不划算的交易。
# 所以这里把所有清理都收敛到两个函数里，并且统一吞掉异常。
# ---------------------------------------------------------------------------


def _keep_runtime() -> bool:
    from . import config

    return bool(getattr(config, "KEEP_RUNTIME_FILES", False))


def _safe_unlink(path: Path):
    """尽力删除一个文件，失败就算了。"""
    try:
        path.unlink()
    except Exception:  # noqa: BLE001
        pass
