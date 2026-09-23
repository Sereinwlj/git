# -*- coding: utf-8 -*-
"""Special Judge：最大子段和（要求同时输出区间）。

为什么这道题必须用 SPJ
----------------------
如果题目只要求输出"最大和是多少"，那用字符串比对就够了。
但这里额外要求输出取得这个和的**区间**，而最优区间往往不唯一，
字符串比对必然把合法的其他区间误判成错误答案。

SPJ 的思路是：不比对答案，而是**验证**答案。

校验三件事（顺序很重要）
------------------------
  (1) 区间下标合法：1 <= l <= r <= len
  (2) 区间实际的和 == 用户声明的和
  (3) 用户声明的和 == 标准答案里的最大值

第 (3) 条是整道题的关键：
前两条只能证明"用户没有自相矛盾"，一个老实的错误解也能通过；
只有第 (3) 条才能证明"用户确实求出了全局最优"。
判构造题、多解题的时候，SPL 一定是"验证 + 比对最优值"两部分都要有的。

调用协议
--------
    python checker.py <输入文件> <用户输出文件> <标准答案文件>

退出码 0 表示通过，非 0 表示不通过；
标准输出/错误输出的内容会被拿去做失败原因提示。
"""

import sys


def fail(reason: str) -> int:
    """统一的不通过出口：把原因写出来，退出码给 1。"""
    print(reason)
    return 1


def main() -> int:
    if len(sys.argv) < 4:
        return fail("SPJ 参数不足，需要：输入文件 用户输出文件 标准答案文件")

    input_path, user_path, answer_path = sys.argv[1], sys.argv[2], sys.argv[3]

    try:
        # ---- 读输入 ----
        tokens = open(input_path, encoding="utf-8").read().split()
        if not tokens:
            return fail("输入文件为空")
        n = int(tokens[0])
        a = [int(x) for x in tokens[1:1 + n]]
        if len(a) != n:
            return fail(f"输入文件损坏：声明 n={n}，实际只有 {len(a)} 个数")

        # ---- 读用户输出 ----
        raw = open(user_path, encoding="utf-8").read().split()
        if len(raw) != 3:
            return fail(f"输出格式不对：需要恰好 3 个整数（和 左端点 右端点），实际得到 {len(raw)} 个")

        try:
            claimed_sum, left, right = int(raw[0]), int(raw[1]), int(raw[2])
        except ValueError:
            return fail(f"三个输出必须都是整数，实际是：{raw}")

        # ---- 校验 (1)：区间是否合法 ----
        if not (1 <= left <= right <= n):
            return fail(f"区间不合法：要求 1 <= l <= r <= {n}，实际得到 l={left}, r={right}")

        # ---- 校验 (2)：区间实际的和是否等于声明的和 ----
        actual = sum(a[left - 1:right])
        if actual != claimed_sum:
            return fail(
                f"区间与声明不符：区间 [{left}, {right}] 的实际和是 {actual}，"
                f"但你声明的是 {claimed_sum}"
            )

        # ---- 校验 (3)：声明的和是否就是最优值 ----
        best = int(open(answer_path, encoding="utf-8").read().split()[0])
        if claimed_sum != best:
            return fail(f"不是最优解：你给出 {claimed_sum}，而正确答案是 {best}")

        # 走到这里说明三条全过，任一最优区间都接受
        print(f"区间 [{left}, {right}] 的和为 {claimed_sum}，等于最优解，接受")
        return 0

    except FileNotFoundError as exc:
        return fail(f"SPJ 读不到文件：{exc}")
    except Exception as exc:  # noqa: BLE001
        return fail(f"SPJ 内部错误：{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    sys.exit(main())
