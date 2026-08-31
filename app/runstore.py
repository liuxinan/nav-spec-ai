# -*- coding: utf-8 -*-
"""运行持久化：每次流水线运行落盘到 runs/<id>/，可回放、可审计。

目录内容：
  DDD.md / DDD.confirmed.md     领域设计文档（生成稿 / 人工确认稿）
  SDD.md / SDD.confirmed.md     软件设计文档（生成稿 / 人工确认稿）
  events.ndjson                 事件日志（追加写入）
  diff.patch                    目标工程的全部代码改动
  result.json                   终态结果（程序化消费）
"""
import json
import os
import threading
import time

_LOCK = threading.Lock()


def runs_dir(root: str) -> str:
    d = os.path.join(root, "runs")
    os.makedirs(d, exist_ok=True)
    return d


def new_run_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def run_dir(root: str, run_id: str) -> str:
    d = os.path.join(runs_dir(root), run_id)
    os.makedirs(d, exist_ok=True)
    return d


def append_event(root: str, run_id: str, stage: str, msg: str, level: str = "info"):
    """追加一条 NDJSON 事件（含内存快照由调用方维护，这里只落盘）。"""
    rec = {"ts": time.strftime("%H:%M:%S"), "stage": stage, "level": level, "msg": msg}
    with _LOCK:
        with open(os.path.join(run_dir(root, run_id), "events.ndjson"),
                  "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def write_doc(root: str, run_id: str, name: str, content: str):
    with _LOCK:
        with open(os.path.join(run_dir(root, run_id), name), "w", encoding="utf-8") as f:
            f.write(content)


def write_diff(root: str, run_id: str, diff: str):
    write_doc(root, run_id, "diff.patch", diff or "(无改动)")


def write_result(root: str, run_id: str, result: dict):
    write_doc(root, run_id, "result.json",
              json.dumps(result, ensure_ascii=False, indent=2))
