# Mini OJ · 迷你在线判题机

一个用 **Python 标准库**实现的在线判题系统（Online Judge）。零第三方依赖，克隆下来就能跑。

它的定位不是一个能承办比赛的工业级 OJ，而是一个**能把判题这件事讲清楚**的完整实现：
编译、沙箱、资源限制、状态机、异步队列、Special Judge，一样不少。

```
┌──────────┐  POST /api/submissions   ┌──────────────┐
│  浏览器   │ ───────────────────────> │  HTTP 线程池  │
│ 轮询结果  │ <─────────────────────── │  （快，不阻塞）│
└──────────┘   GET /api/submissions/N └──────┬───────┘
                                             │ 落库 + 入队
                                             v
                                     ┌──────────────┐
                                     │  任务队列     │
                                     └──────┬───────┘
                                            │
                        ┌───────────────────┼───────────────────┐
                        v                   v                   v
                   ┌─────────┐         ┌─────────┐         ┌─────────┐
                   │ worker1 │         │ worker2 │   ...   │ workerN │
                   └────┬────┘         └────┬────┘         └────┬────┘
                        │  编译 -> 沙箱运行 -> 比对 -> 落库      │
                        v                   v                   v
                   ┌────────────────────────────────────────────┐
                   │              沙箱（Local / Docker）          │
                   │      时间限制 · 输出限制 · 内存限制 · 隔离     │
                   └────────────────────────────────────────────┘
```

---

## 特性

- **异步判题**：提交立刻返回 ID，判题在后台 worker 池里跑，前端轮询结果
- **完整状态机**：AC / WA / TLE / MLE / OLE / RE / CE / SE，能准确区分失败原因
- **三种比对器**：精确比对、浮点误差比对、**Special Judge（外部脚本）**
- **资源限制**：时间（父进程监控 + CPU 时间）、内存、输出体积
- **两级沙箱**：本地进程沙箱（跨平台）/ Docker 容器沙箱（强隔离），同一接口可切换
- **服务恢复**：进程重启后自动捞回没判完的提交，不会永久卡在"等待判题"
- **零依赖**：只用标准库，不需要 pip 装任何东西

---

## 快速开始

```bash
cd oj
python run.py
```

然后打开 <http://127.0.0.1:8000>。

常用参数：

```bash
python run.py --port 9000          # 换端口
python run.py --workers 8          # 调大并发判题数（默认 4）
python run.py --sandbox docker     # 用容器沙箱（需要 Docker）
```

环境变量：

| 变量 | 作用 | 默认 |
|---|---|---|
| `OJ_KEEP_RUNTIME` | 设为 `1` 则保留判题现场文件（排查误判用） | 关闭 |
| `OJ_SANDBOX` | `local` / `docker` | `local` |
| `OJ_WORKERS` | worker 数量 | `4` |
| `OJ_DOCKER_IMAGE` | 容器沙箱镜像 | `gcc:13` |

**前置条件**：需要 `g++` 在 PATH 里（用于编译 C++ 提交）。Python 提交用当前解释器。

---

## 目录结构

```
oj/
├── run.py                      启动入口
├── oj/
│   ├── config.py               全部可调参数
│   ├── models.py               判题状态机、题目、结果数据结构
│   ├── store.py                SQLite 存储层
│   ├── sandbox.py              沙箱层（Local / Docker）
│   ├── judge.py                判题核心（编译 -> 运行 -> 比对）
│   ├── service.py              提交队列 + worker 池
│   ├── server.py               HTTP 接口
│   └── static/index.html       前端页面
├── problems/                   题库（文件即数据）
│   ├── 1000-a-plus-b/
│   │   ├── problem.json
│   │   └── data/{1.in,1.out,...}
│   ├── 1001-float-sum/
│   └── 1002-max-subarray/
│       ├── problem.json
│       ├── checker.py          ← Special Judge
│       └── data/
└── tests/smoke_test.py         端到端测试
```

---

## 判题流程

1. 用户提交 -> 落库（状态 `Pending`）-> 入队 -> 立刻返回 submission id
2. worker 取出任务 -> 状态改 `Judging` -> 在 `runtime/submission_<id>/` 下建独立工作目录
3. 写源码 -> 编译（编译型语言）-> 编译失败直接 `CompileError`，不再跑测试点
4. 逐测试点执行：喂输入 -> 沙箱运行 -> 比对输出
5. 遇到第一个不通过的点即停止，把"第几个点挂的、为什么挂"写进 message
6. 结果落库，状态变终态，前端轮询到终态后停止

---

## 支持的语言

| id | 名称 | 编译命令 | 运行命令 |
|---|---|---|---|
| `cpp` | C++17 | `g++ -O2 -std=c++17 -o main main.cpp` | `./main` |
| `python` | Python 3 | 无需编译 | `python main.py` |

新增语言只需要在 `oj/judge.py` 的 `LANGUAGES` 字典里加一条，判题流程完全不用改。

---

## HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 前端页面 |
| GET | `/api/languages` | 支持的提交语言 |
| GET | `/api/problems` | 题目列表 |
| GET | `/api/problems/{id}` | 题目详情 |
| POST | `/api/submissions` | 提交代码，返回 `{"id": N}`（202） |
| GET | `/api/submissions/{id}` | 查询判题结果 |
| GET | `/api/submissions?problem_id=&limit=` | 提交记录列表 |

提交示例：

```bash
curl -X POST http://127.0.0.1:8000/api/submissions \
  -H "Content-Type: application/json" \
  -d '{"problem_id":"1000","language":"cpp","source_code":"#include <iostream>\nint main(){long long a,b;std::cin>>a>>b;std::cout<<a+b;}"}'
```

---

## 题库格式

`problems/<目录名>/problem.json`：

```json
{
  "id": "1000",
  "title": "A + B Problem",
  "description": "题面（支持换行）",
  "time_limit_ms": 1000,
  "memory_limit_mb": 256,
  "output_limit_kb": 1024,
  "checker": { "type": "exact" }
}
```

三种 `checker`：

| type | 参数 | 适用 |
|---|---|---|
| `exact` | 无 | 绝大多数题，逐行比对（忽略行尾空格） |
| `float` | `eps` | 浮点答案题 |
| `script` | `path` | Special Judge，多解题 / 构造题 |

测试点放在同目录 `data/` 下，`N.in` 与 `N.out` 自动配对，按数字排序。

### 关于 Special Judge

`script` 类型的 checker 会以如下协议被调用：

```
python checker.py <输入文件> <用户输出文件> <标准答案文件>
退出码 0 = 通过，非 0 = 不通过，stdout 作为失败原因
```

仓库里的 `problems/1002-max-subarray/checker.py` 是一个完整的范例：
它校验「区间合法 + 区间和等于声明值 + 声明值等于最优解」三件事，
所以"输出任意一个最优区间"这种多解题才能被正确判定。

---

## 沙箱

### LocalSandbox（默认）

直接开本机子进程。**能挡住的**：死循环、无限输出；POSIX 下额外能挡住 CPU 时间超限、虚拟内存超限、fork 炸弹。
**挡不住的**：读写宿主文件系统、网络外联。所以它只适合本地开发和演示。

### DockerSandbox

一次收口 `--network none` / `--read-only` / `--pids-limit` / `--memory` / `--memory-swap` / `-u nobody` / `no-new-privileges`。
**要上线就应该用这个。**

```bash
python run.py --sandbox docker
```

如果配置了 docker 但 Docker 不可用，服务会自动降级到 local 并打印警告 —— 可用性优先。

---

## 测试

```bash
python run.py                      # 终端 1：启动服务
python tests/smoke_test.py         # 终端 2：跑端到端测试
```

测试会真实触发全部 10 种场景（正确解、答案错误、编译错误、超时、输出超限、运行错误、
Python 提交、SPJ 三种情况），报告同时写入 `tests/report.txt`。

---

## 已知不足

这些不是"没做完"，而是**有意识的取舍**，每一条都可以展开讲：

1. **内存统计不精确**。本地沙箱用的是 `RUSAGE_CHILDREN.ru_maxrss`，它是所有子进程的历史峰值、
   只增不减，并发判题时会偏大。要准确必须读 cgroup v2 的 `memory.peak`。
2. **内存超限的判定是启发式的**。进程被资源限制挡住时，退出码并不统一（可能是 `SIGABRT`、`SIGSEGV`，
   也可能是抛异常后正常退出），所以只能结合 stderr 关键字和信号号来猜。
3. **队列在内存里**。进程重启后队列丢失，靠数据库里的 `Pending/Judging` 记录重建。
   提交量大的话应该换成 Redis / RabbitMQ。
4. **没有用户系统和权限控制**。任何人都能提交，也没有限流，一个脚本就能刷爆队列。
5. **没有实时推送**。前端靠 500ms 轮询，应该换成 SSE 或 WebSocket。
6. **编译器不在沙箱里**。编译器需要读写自己的头文件和临时目录，塞进沙箱成本很高，
   目前只用超时兜底 —— 这是真实 OJ 也普遍接受的取舍。

---

## 下一步可以做什么

- 接入评测数据校验（防止测试点本身是错的）
- 加上限流与用户认证
- 用 Redis 替换内存队列，做多机判题
- 用 cgroup v2 做精确的内存与 CPU 统计
- 支持交互题（需要 checker 与用户程序双向通信）
- 支持重判、比分榜、题目标签

---

## 许可

供学习与面试准备使用。
