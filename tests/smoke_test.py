# -*- coding: utf-8 -*-
"""端到端冒烟测试。

用途：服务跑起来之后，把「所有判题状态」都真实触发一遍，
确认判题链路（编译 -> 沙箱运行 -> 比对 -> 落库 -> 接口返回）是通的。

为什么值得单独写一个测试脚本
---------------------------
判题机这类系统的核心价值就在"能不能正确地区分出各种失败原因"。
一个只能判 AC/WA 的 OJ 是没有用的 —— 用户真正需要知道的是
"我到底是超时了、还是超内存了、还是输出格式不对"。
所以测试必须覆盖到每一种状态，而不只是"正确解能过"。

用法：
    1. 另开一个终端启动服务：python run.py
    2. 运行本脚本：python tests/smoke_test.py
"""

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import List

BASE = "http://127.0.0.1:8000"

# 测试报告落盘位置（UTF-8），方便贴进 CI 日志或存档
REPORT_PATH = Path(__file__).resolve().parent / "report.txt"


# ---------------------------------------------------------------------------
# 测试用例：每一组是 (题目, 语言, 说明, 期望状态, 代码)
# ---------------------------------------------------------------------------

CPP_OK = """#include <bits/stdc++.h>
using namespace std;
int main(){ long long a,b; if(!(cin>>a>>b)) return 0; cout << a+b << endl; return 0; }
"""

CPP_WA = """#include <bits/stdc++.h>
using namespace std;
int main(){ long long a,b; if(!(cin>>a>>b)) return 0; cout << a-b << endl; return 0; }
"""

CPP_CE = """#include <bits/stdc++.h>
int main(){ 这 不 是 合 法 的 C++ ; }
"""

CPP_TLE = """#include <bits/stdc++.h>
int main(){ volatile long long x=0; while(true) x++; return 0; }
"""

CPP_OLE = """#include <bits/stdc++.h>
using namespace std;
int main(){
    string s(4096, 'A');
    while(true) fwrite(s.data(), 1, s.size(), stdout);   // 疯狂输出，触发输出超限
    return 0;
}
"""

CPP_RE = """#include <bits/stdc++.h>
int main(){ return 3; }
"""

PY_OK = """n = int(input())
xs = [float(x) for x in input().split()]
print("%.6f" % sum(xs))
"""

CPP_SUBARRAY_OK = """#include <bits/stdc++.h>
using namespace std;
int main(){
    int n; cin >> n;
    vector<long long> a(n);
    for (auto &x : a) cin >> x;
    long long best = LLONG_MIN, cur = 0, l = 0, bl = 0, br = 0;
    for (int i = 0; i < n; i++){
        if (cur <= 0){ cur = a[i]; l = i; } else cur += a[i];
        if (cur > best){ best = cur; bl = l; br = i; }
    }
    cout << best << " " << bl+1 << " " << br+1 << endl;
    return 0;
}
"""

# 合法区间但和不是最大 -> SPJ 必须判它错
CPP_SUBARRAY_SUBOPT = """#include <bits/stdc++.h>
using namespace std;
int main(){
    int n; cin >> n;
    vector<long long> a(n);
    for (auto &x : a) cin >> x;
    cout << a[0] << " 1 1" << endl;   // 直接输出第一个元素，几乎肯定不是最优
    return 0;
}
"""

# 自相矛盾：声明的和与区间实际的和对不上 -> SPJ 必须抓出来
CPP_SUBARRAY_LIE = """#include <bits/stdc++.h>
using namespace std;
int main(){
    int n; cin >> n;
    vector<long long> a(n);
    for (auto &x : a) cin >> x;
    cout << 999999999 << " 1 " << n << endl;
    return 0;
}
"""

CASES = [
    ("1000", "cpp",    "正确解",            "Accepted",            CPP_OK),
    ("1000", "cpp",    "答案错误（输出了 a-b）", "WrongAnswer",        CPP_WA),
    ("1000", "cpp",    "编译错误",          "CompileError",        CPP_CE),
    ("1000", "cpp",    "死循环（应判超时）",  "TimeLimitExceeded",   CPP_TLE),
    ("1000", "cpp",    "无限输出（应判输出超限）", "OutputLimitExceeded", CPP_OLE),
    ("1000", "cpp",    "非零退出码",        "RuntimeError",        CPP_RE),
    ("1001", "python", "Python 浮点题",     "Accepted",            PY_OK),
    ("1002", "cpp",    "SPJ：最优区间",      "Accepted",            CPP_SUBARRAY_OK),
    ("1002", "cpp",    "SPJ：合法但非最优",  "WrongAnswer",         CPP_SUBARRAY_SUBOPT),
    ("1002", "cpp",    "SPJ：和与区间不符",  "WrongAnswer",         CPP_SUBARRAY_LIE),
]


# ---------------------------------------------------------------------------
# HTTP 工具
# ---------------------------------------------------------------------------


def post_json(path, payload):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        BASE + path, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_json(path):
    with urllib.request.urlopen(BASE + path, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def submit_and_wait(problem_id, language, code, timeout_sec=40):
    """提交并轮询到终态。"""
    created = post_json("/api/submissions", {
        "problem_id": problem_id,
        "language": language,
        "source_code": code,
    })
    sid = created["id"]

    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        sub = get_json(f"/api/submissions/{sid}")
        if sub["is_final"]:
            return sub
        time.sleep(0.3)
    raise TimeoutError(f"提交 #{sid} 在 {timeout_sec}s 内没有出结果")


def check_server():
    try:
        get_json("/api/problems")
    except urllib.error.URLError as exc:
        print(f"连不上服务 {BASE}：{exc}")
        print("请先在另一个终端执行：python run.py")
        return False
    return True


def main() -> int:
    # 同时把报告写进文件。原因很实际：日志被重定向时，
    # 控制台编码经常把中文搞成乱码，而文件里始终是干净的 UTF-8。
    lines: List[str] = []

    def emit(text: str = ""):
        print(text)
        lines.append(text)

    if not check_server():
        return 2

    problems = get_json("/api/problems")
    emit(f"服务正常，题库共 {len(problems)} 道题：")
    for p in problems:
        emit(f"  - {p['id']}  {p['title']}  ({p['time_limit_ms']}ms / {p['memory_limit_mb']}MB)")
    emit()

    emit(f"{'题目':<8}{'语言':<9}{'用例':<26}{'期望':<24}{'实际':<24}{'结果':<8}耗时")
    emit("-" * 110)

    passed = failed = 0
    for problem_id, language, desc, expect, code in CASES:
        sub = None
        try:
            sub = submit_and_wait(problem_id, language, code)
            actual = sub["status"]
        except Exception as exc:  # noqa: BLE001
            actual = f"<异常 {type(exc).__name__}>"

        ok = actual == expect
        passed += ok
        failed += (not ok)
        elapsed = f"{sub['time_ms']} ms" if sub else "-"
        emit(
            f"{problem_id:<8}{language:<9}{desc:<26}{expect:<24}{actual:<24}"
            f"{'PASS' if ok else 'FAIL':<8}{elapsed}"
        )
        if not ok and sub:
            emit(f"       ↳ 判题信息：{sub.get('message', '')[:800]}")

    emit()
    emit(f"合计：{passed} 通过 / {failed} 失败，共 {len(CASES)} 项")

    try:
        REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"\n报告已写入：{REPORT_PATH}")
    except OSError:
        pass

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
