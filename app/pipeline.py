# -*- coding: utf-8 -*-
"""流水线状态机（人工确认门禁 + 有界重试）。

一句话需求 → 项目扫描 → DDD 生成 → [人工确认①] → SDD 生成 → [人工确认②]
→ 自动编码 → 编译仲裁（失败回灌，≤compileRetries）→ 规约评审（判负回灌，
≤reviewRetries）→ 终态。

终态 outcome：ok / compile_failed / review_failed / error
（沿用参考工程退出码语义：0/1/2/3）
"""
import json
import os
import threading
import subprocess

from . import runstore
from .llm import LLMClient, LLMError
from . import stages

# 阶段顺序（前端时间线同序）
STAGE_ORDER = ["scan", "ddd", "gate_ddd", "sdd", "gate_sdd",
               "code", "compile", "review", "final"]

_OUTCOME_TEXT = {
    "ok": "✅ 流水线成功：规约通过编译与评审，代码已交付",
    "compile_failed": "❌ 编译仲裁未通过（重试已用尽）",
    "review_failed": "❌ 规约评审未通过（重试已用尽）",
    "error": "💥 流水线内部错误",
    "rejected": "⛔ 文档被人工驳回且重生成次数用尽",
    "interrupted": "⏸ 运行被服务重启中断（可重新提交需求，本次记录仅存档）",
}


class Run:
    """一次流水线运行的全部内存状态（前端轮询的数据源）。"""

    def __init__(self, run_id: str, requirement: str):
        self.id = run_id
        self.requirement = requirement
        self.stage = "scan"           # 当前阶段
        self.status = "running"       # running / waiting / done
        self.gate = None              # "ddd" / "sdd"（人工确认门禁）
        self.outcome = None           # 终态
        self.docs = {}                # 生成稿 {"ddd": md, "sdd": md}
        self.confirmed = {}           # 人工确认稿
        self.diff = ""
        self.code_summary = ""
        self.verdict = None           # 评审裁决
        self.events = []              # [{ts, stage, level, msg}]
        self.counters = {"ddd_regen": 0, "sdd_regen": 0,
                         "compile_retry": 0, "review_retry": 0, "code_runs": 0}
        # 门禁信令
        self._gate_event = threading.Event()
        self.gate_action = None       # confirm / reject
        self.gate_content = None      # 确认时（可能被人工编辑过的）文档内容
        self.gate_feedback = None     # 驳回意见
        self._lock = threading.Lock()

    def log(self, stage: str, msg: str, level: str = "info"):
        rec = {"ts": _now(), "stage": stage, "level": level, "msg": msg}
        with self._lock:
            self.events.append(rec)
            if len(self.events) > 600:
                self.events = self.events[-600:]
        runstore.append_event(_ROOT, self.id, stage, msg, level)
        print("[%s][%s] %s" % (rec["ts"], stage, msg), flush=True)

    def set_stage(self, stage: str):
        self.stage = stage
        if stage.startswith("gate_"):
            self.status = "waiting"
            self.gate = stage[len("gate_"):]
        else:
            self.status = "running"
            self.gate = None

    def snapshot(self) -> dict:
        """给前端的完整状态快照。"""
        with self._lock:
            events = list(self.events)
        steps = []
        idx = STAGE_ORDER.index(self.stage) if self.stage in STAGE_ORDER else len(STAGE_ORDER)
        for i, s in enumerate(STAGE_ORDER):
            state = "done" if i < idx else ("active" if i == idx else "todo")
            steps.append({"key": s, "state": state})
        return {
            "id": self.id,
            "requirement": self.requirement,
            "stage": self.stage,
            "status": self.status,
            "gate": self.gate,
            "outcome": self.outcome,
            "outcomeText": _OUTCOME_TEXT.get(self.outcome, ""),
            "steps": steps,
            "docs": self.docs,
            "confirmed": self.confirmed,
            "gateDoc": self.docs.get(self.gate or "", ""),
            "diff": self.diff,
            "codeSummary": self.code_summary,
            "verdict": self.verdict,
            "counters": self.counters,
            "events": events[-200:],
        }


def _now():
    import time
    return time.strftime("%H:%M:%S")


_ROOT = "."  # 由 Pipeline 初始化时设置（工程根，用于 runs/ 落盘）


class Pipeline:
    """运行注册表 + 工作线程调度。"""

    def __init__(self, cfg: dict, root: str):
        global _ROOT
        _ROOT = root
        self.cfg = cfg
        self.root = root
        self.client = LLMClient(cfg["llm"])
        self.runs = {}
        self._lock = threading.Lock()
        self._load_persisted()  # 重启后从 runs/ 恢复历史运行（支持回放）

    # ---------------- 历史运行恢复（回放） ----------------
    def _load_persisted(self):
        base = runstore.runs_dir(self.root)
        for run_id in sorted(os.listdir(base)):
            d = os.path.join(base, run_id)
            if not os.path.isdir(d) or run_id in self.runs:
                continue
            try:
                self.runs[run_id] = self._hydrate(run_id, d)
            except Exception:
                continue  # 坏目录直接跳过，不阻塞启动

    def _hydrate(self, run_id: str, d: str) -> Run:
        """从磁盘目录重建 Run（只读回放态，不再驱动任何流程）。"""
        def read(name):
            p = os.path.join(d, name)
            if os.path.isfile(p):
                with open(p, "r", encoding="utf-8") as f:
                    return f.read()
            return ""

        run = Run(run_id, "")
        # 事件
        for line in read("events.ndjson").splitlines():
            if not line.strip():
                continue
            try:
                run.events.append(json.loads(line))
            except Exception:
                pass
        # 终态与字段
        result = {}
        raw = read("result.json")
        if raw:
            try:
                result = json.loads(raw)
            except Exception:
                result = {}
        run.requirement = result.get("requirement", "")
        if not run.requirement and run.events:
            first = run.events[0].get("msg", "")
            if first.startswith("运行创建："):
                run.requirement = first[len("运行创建："):]
        run.outcome = result.get("outcome") or "interrupted"
        run.counters.update(result.get("counters") or {})
        run.code_summary = result.get("codeSummary", "")
        run.verdict = result.get("verdict")
        run.diff = result.get("diff", "")
        # 文档（生成稿 + 确认稿）
        for key, gen, conf in (("ddd", "DDD.md", "DDD.confirmed.md"),
                               ("sdd", "SDD.md", "SDD.confirmed.md")):
            run.docs[key] = read(gen)
            run.confirmed[key] = read(conf)
        run.stage = "final"
        run.status = "done"
        return run

    # ---------------- 对外 API ----------------
    def create_run(self, requirement: str) -> Run:
        requirement = (requirement or "").strip()
        if not requirement:
            raise ValueError("需求不能为空")
        run_id = runstore.new_run_id()
        run = Run(run_id, requirement)
        with self._lock:
            self.runs[run_id] = run
        run.log("scan", "运行创建：%s" % requirement[:80])
        threading.Thread(target=self._worker, args=(run,), daemon=True).start()
        return run

    def get_run(self, run_id: str):
        return self.runs.get(run_id)

    def list_runs(self):
        return [{"id": r.id, "requirement": r.requirement,
                 "stage": r.stage, "status": r.status, "outcome": r.outcome}
                for r in sorted(self.runs.values(), key=lambda r: r.id, reverse=True)]

    def submit_gate(self, run_id: str, action: str, content: str = "",
                    feedback: str = ""):
        """人工确认/驳回（HTTP 线程调用）。"""
        run = self.runs.get(run_id)
        if not run or not run.gate or run.status != "waiting":
            raise ValueError("当前不在人工确认环节")
        if action not in ("confirm", "reject"):
            raise ValueError("action 必须是 confirm 或 reject")
        if action == "reject" and not feedback.strip():
            raise ValueError("驳回必须填写意见（会回灌给生成阶段）")
        run.gate_action = action
        run.gate_content = content
        run.gate_feedback = feedback
        run._gate_event.set()
        return True

    def resume_run(self, run_id: str) -> Run:
        """从失败处恢复流水线：跳过 DDD/SDD 生成与人工确认，直接进入编码→编译→评审闭环。
        省 LLM 调用 + token + 人工确认时间。"""
        run = self.runs.get(run_id)
        if not run:
            raise ValueError("运行不存在")
        if run.status != "done":
            raise ValueError("运行尚未结束，无法恢复")
        if run.outcome in (None, "ok", "rejected", "interrupted"):
            raise ValueError("该运行未失败或已成功，无需恢复")
        if not run.confirmed.get("ddd") or not run.confirmed.get("sdd"):
            raise ValueError("DDD 或 SDD 未确认，无法恢复（需重新提交需求）")
        # 重置状态：保留 DDD/SDD 确认稿与事件历史，重置闭环阶段
        run.outcome = None
        run.status = "running"
        run.gate = None
        run.verdict = None
        run.diff = ""
        run.code_summary = ""
        run.counters["compile_retry"] = 0
        run.counters["review_retry"] = 0
        run.counters["code_runs"] = 0
        run.gate_action = run.gate_content = run.gate_feedback = None
        run.log("code", "从失败处恢复流水线（跳过 DDD/SDD 生成与人工确认，直接进入编码→编译→评审闭环）")
        threading.Thread(target=self._worker, args=(run, True), daemon=True).start()
        return run

    # ---------------- 工作线程 ----------------
    def _worker(self, run: Run, resume: bool = False):
        try:
            self._execute(run, resume=resume)
        except Exception as e:
            run.log("final", "流水线异常：%s" % e, "error")
            run.outcome = "error"
        finally:
            run.status = "done"
            run.gate = None
            runstore.write_result(self.root, run.id, self._result_of(run))
            run.log("final", "终态：%s" % (run.outcome or "ok"))

    def _result_of(self, run: Run) -> dict:
        return {
            "runId": run.id,
            "requirement": run.requirement,
            "outcome": run.outcome,
            "counters": run.counters,
            "codeSummary": run.code_summary,
            "verdict": run.verdict,
            "diff": run.diff,
            "docs": {"ddd": run.confirmed.get("ddd", run.docs.get("ddd", "")),
                     "sdd": run.confirmed.get("sdd", run.docs.get("sdd", ""))},
        }

    def _execute(self, run: Run, resume: bool = False):
        cfg = self.cfg
        limits = cfg["limits"]
        workdir = cfg["target"]["workdir"]

        if not resume:
            # ---- 阶段0：扫描 ----
            run.set_stage("scan")
            run.log("scan", "扫描目标工程：%s" % workdir)
            scan = stages.scan_project(workdir)
            run.log("scan", "扫描完成：识别到 %d 行上下文" % len(scan.splitlines()))

            # ---- 阶段1：DDD 生成 + 人工门禁① ----
            ddd_feedback = ""
            while True:
                run.set_stage("ddd")
                run.log("ddd", "生成 DDD 领域设计文档…")
                ddd_md = stages.gen_ddd(self.client, run.requirement, scan, ddd_feedback)
                run.docs["ddd"] = ddd_md
                runstore.write_doc(self.root, run.id, "DDD.md", ddd_md)
                run.log("ddd", "DDD 文档已生成（%d 字），等待人工确认" % len(ddd_md))
                run.set_stage("gate_ddd")
                action, content, feedback = self._wait_gate(run, "ddd")
                if action == "confirm":
                    run.confirmed["ddd"] = (content or "").strip() or ddd_md
                    runstore.write_doc(self.root, run.id, "DDD.confirmed.md",
                                       run.confirmed["ddd"])
                    run.log("gate_ddd", "DDD 已人工确认（含可能的人工修订）")
                    break
                # 驳回 → 有界重生成
                run.counters["ddd_regen"] += 1
                if run.counters["ddd_regen"] > limits["regenLimit"]:
                    run.outcome = "rejected"
                    run.log("gate_ddd", "DDD 驳回次数超限（%d）" % limits["regenLimit"], "error")
                    return
                ddd_feedback = feedback
                run.log("gate_ddd", "DDD 被驳回（第 %d 次），意见回灌重新生成"
                        % run.counters["ddd_regen"], "warn")

            # ---- 阶段2：SDD 生成 + 人工门禁② ----
            sdd_feedback = ""
            while True:
                run.set_stage("sdd")
                run.log("sdd", "基于已确认的 DDD 生成 SDD 软件设计文档…")
                sdd_md = stages.gen_sdd(self.client, run.requirement,
                                        run.confirmed["ddd"], scan, sdd_feedback)
                run.docs["sdd"] = sdd_md
                runstore.write_doc(self.root, run.id, "SDD.md", sdd_md)
                run.log("sdd", "SDD 文档已生成（%d 字），等待人工确认" % len(sdd_md))
                run.set_stage("gate_sdd")
                action, content, feedback = self._wait_gate(run, "sdd")
                if action == "confirm":
                    run.confirmed["sdd"] = (content or "").strip() or sdd_md
                    runstore.write_doc(self.root, run.id, "SDD.confirmed.md",
                                       run.confirmed["sdd"])
                    run.log("gate_sdd", "SDD 已人工确认，放行自动编码（此后不再人工介入）")
                    break
                run.counters["sdd_regen"] += 1
                if run.counters["sdd_regen"] > limits["regenLimit"]:
                    run.outcome = "rejected"
                    run.log("gate_sdd", "SDD 驳回次数超限（%d）" % limits["regenLimit"], "error")
                    return
                sdd_feedback = feedback
                run.log("gate_sdd", "SDD 被驳回（第 %d 次），意见回灌重新生成"
                        % run.counters["sdd_regen"], "warn")
        # resume=True 时：DDD/SDD 已确认，直接进入编码→编译→评审闭环

        # ---- 阶段3~5：编码 → 编译 → 评审（有界回灌闭环） ----
        feedback = ""
        while True:
            run.set_stage("code")
            run.counters["code_runs"] += 1
            run.log("code", "编码 Agent 启动（第 %d 次），按 SDD 修改目标工程…"
                    % run.counters["code_runs"])
            summary = stages.run_code_agent(
                self.client, workdir, run.confirmed["sdd"], run.requirement,
                feedback, log=lambda m: run.log("code", m))
            run.code_summary = summary
            run.log("code", "编码完成：%s" % summary.replace("\n", " | ")[:300])

            # 修改感知：git diff
            run.diff = self._git_diff(workdir)
            runstore.write_diff(self.root, run.id, run.diff)
            run.log("code", "git diff 已捕获（%d 行）" % len(run.diff.splitlines()))

            # 编译：客观仲裁
            run.set_stage("compile")
            run.log("compile", "执行编译/验证：%s" % cfg["target"]["compileCmd"])
            ok, output, errors = stages.run_compile(workdir, cfg["target"]["compileCmd"])
            if ok:
                run.log("compile", "编译通过（客观仲裁 OK）")
            else:
                run.counters["compile_retry"] += 1
                if run.counters["compile_retry"] > limits["compileRetries"]:
                    run.outcome = "compile_failed"
                    run.log("compile", "编译失败且重试用尽", "error")
                    return
                feedback = "上一轮编译失败，请增量修复，不要重做无关内容。编译错误：\n%s" % errors
                run.log("compile", "编译失败 → 错误结构化回灌（第 %d/%d 次重试）"
                        % (run.counters["compile_retry"], limits["compileRetries"]), "warn")
                continue

            # 评审：以 SDD 为基准的主观仲裁
            run.set_stage("review")
            verdict = stages.run_review(
                self.client, run.confirmed["sdd"], run.diff, run.code_summary,
                log=lambda m: run.log("review", m))
            run.verdict = verdict
            if verdict["pass"]:
                run.outcome = "ok"
                run.log("review", "评审通过：实现符合规约，流水线成功收口")
                return
            run.counters["review_retry"] += 1
            if run.counters["review_retry"] > limits["reviewRetries"]:
                run.outcome = "review_failed"
                run.log("review", "评审判负且重试用尽", "error")
                return
            feedback = "上一轮规约评审不通过，请按以下意见增量修复：\n- %s" % "\n- ".join(
                verdict["issues"] if isinstance(verdict.get("issues"), list) else [str(verdict)])
            run.log("review", "评审判负 → 意见结构化回灌（第 %d/%d 次重试）"
                    % (run.counters["review_retry"], limits["reviewRetries"]), "warn")

    # ---------------- 门禁等待 ----------------
    def _wait_gate(self, run: Run, doc_name: str):
        run._gate_event.clear()
        run._gate_event.wait()  # 挂起直到人工在页面上确认/驳回
        action = run.gate_action
        content = run.gate_content
        feedback = (run.gate_feedback or "").strip()
        run.gate_action = run.gate_content = run.gate_feedback = None
        if action == "confirm" and not (content or "").strip():
            content = run.docs.get(doc_name, "")
        return action, content, feedback

    # ---------------- git 工具 ----------------
    def _git_diff(self, workdir: str) -> str:
        """修改感知：git add -A 后取 staged diff。
        排除 docs/ 目录（领域知识库文件不是编码 Agent 的改动）。"""
        for cmd in (["git", "add", "-A"],):
            subprocess.run(cmd, cwd=workdir, capture_output=True, text=True,
                           timeout=60, encoding="utf-8", errors="replace")
        r = subprocess.run(
            ["git", "diff", "--cached", "HEAD", "--", ".", ":(exclude)docs/"],
            cwd=workdir, capture_output=True, text=True, timeout=60,
            encoding="utf-8", errors="replace")
        diff = r.stdout or ""
        return diff if diff.strip() else "(目标工程无改动)"
