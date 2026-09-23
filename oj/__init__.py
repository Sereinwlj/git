# -*- coding: utf-8 -*-
"""一个用 Python 标准库实现的迷你在线判题机（OJ）。

模块划分
--------
models.py   领域模型：判题状态机、题目、提交结果
store.py    存储层：SQLite 读写
sandbox.py  沙箱层：把用户程序在受控环境里跑起来
judge.py    判题核心：编译 -> 逐测试点运行 -> 比对（含 Special Judge）
service.py  服务层：提交队列 + worker 池
server.py   HTTP 接口与前端页面

依赖方向是单向的：server -> service -> judge -> sandbox，各层都只依赖 store 和 models。
"""

__version__ = "0.1.0"
