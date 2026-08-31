# -*- coding: utf-8 -*-
"""流水线各阶段：扫描 / DDD 生成 / SDD 生成 / 编译仲裁 / 规约对照评审。

设计哲学（继承自参考工程）：
- 客观先于主观：先编译（工具链不会撒谎）后评审（LLM 主观判断）
- 反馈要结构化：编译错误 / 评审 issues 包装成"模型看一眼就能用"的格式回灌
- LLM 只提案不裁决：不确定的业务阈值在文档中明确标注"待人工确认"
"""
import json
import os
import re
import subprocess

from .llm import LLMClient, LLMError
from .agent import LiteAgent
from .tools import ToolBox

STD_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "standards")


def load_standard(name: str) -> str:
    path = os.path.join(STD_DIR, name)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    return ""


# ================= 阶段0：轻量项目扫描 =================
def scan_project(workdir: str) -> str:
    """扫描目标工程：目录结构、README、技术栈嗅探——让生成的 Spec 贴合实际。"""
    parts = []
    box = ToolBox(workdir)
    listing = box._list_dir(".")
    parts.append("【目录结构（两层）】\n%s" % listing)

    for name in ("README.md", "README.MD", "readme.md", "README.txt"):
        p = os.path.join(workdir, name)
        if os.path.isfile(p):
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                readme = f.read(3000)
            parts.append("【README 摘要】\n%s" % readme)
            break

    stack = []
    if os.path.isfile(os.path.join(workdir, "package.json")):
        try:
            with open(os.path.join(workdir, "package.json"), "r", encoding="utf-8") as f:
                pkg = json.load(f)
            deps = list((pkg.get("dependencies") or {}).keys())
            stack.append("前端/Node 工程：package.json，主要依赖 %s" % (", ".join(deps[:15]) or "无"))
            scripts = list((pkg.get("scripts") or {}).keys())
            if scripts:
                stack.append("可用脚本：%s" % ", ".join(scripts[:15]))
        except Exception:
            stack.append("package.json 解析失败")
    if os.path.isfile(os.path.join(workdir, "requirements.txt")):
        stack.append("Python 工程：requirements.txt")
    if any(os.path.isfile(os.path.join(workdir, f)) for f in
           ("tsconfig.json", "vite.config.js", "vite.config.ts")):
        stack.append("疑似 TypeScript/Vite 工程")
    if os.path.isfile(os.path.join(workdir, "index.html")):
        stack.append("含 index.html（可能是纯前端网页工程）")
    parts.append("【技术栈嗅探】\n%s" % ("\n".join(stack) or "未识别出明显技术栈特征"))

    # 领域知识库（docs/ 目录）：轻量 RAG——让生成的规约贴合工程实际业务语义
    docs_dir = os.path.join(workdir, "docs")
    if os.path.isdir(docs_dir):
        kb_parts = []
        for fname in sorted(os.listdir(docs_dir)):
            if not fname.lower().endswith((".md", ".txt")):
                continue
            fpath = os.path.join(docs_dir, fname)
            if not os.path.isfile(fpath):
                continue
            with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                content = f.read(12000)  # 单文件上限，防止上下文爆炸
            kb_parts.append("--- %s ---\n%s" % (fname, content))
        if kb_parts:
            parts.append("【领域知识库（docs/ 目录，作为规约生成的领域上下文）】\n%s"
                         % "\n\n".join(kb_parts))

    return "\n\n".join(parts)


# ================= 阶段1：DDD 文档生成 =================
def gen_ddd(client: LLMClient, requirement: str, scan: str,
            feedback: str = "") -> str:
    system = load_standard("ddd_template.md")
    user = (
        "【一句话需求】\n%s\n\n【目标工程扫描】\n%s\n" % (requirement, scan[:6000])
    )
    if feedback:
        user += "\n【人工驳回意见（必须逐条回应）】\n%s\n" % feedback
    user += "\n请生成 DDD 领域设计文档（Markdown）。"
    return client.complete([
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ])


# ================= 阶段2：SDD 文档生成 =================
def gen_sdd(client: LLMClient, requirement: str, ddd_md: str,
            scan: str, feedback: str = "") -> str:
    system = load_standard("sdd_template.md")
    user = (
        "【一句话需求】\n%s\n\n【已人工确认的 DDD 文档】\n%s\n\n【目标工程扫描】\n%s\n"
        % (requirement, ddd_md, scan[:6000])
    )
    if feedback:
        user += "\n【人工驳回意见（必须逐条回应）】\n%s\n" % feedback
    user += "\n请基于以上 DDD 文档生成 SDD 软件设计文档（Markdown）。"
    return client.complete([
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ])


# ================= 阶段3：按规约自动编码 =================
def run_code_agent(client: LLMClient, workdir: str, sdd_md: str,
                   requirement: str, extra_feedback: str,
                   log=lambda msg: None) -> str:
    """编码 Agent 直接修改目标仓库（编排层不碰文件，只绑定 cwd）。"""
    system = load_standard("coding.md")
    user = (
        "【一句话需求】\n%s\n\n【已人工确认的 SDD 设计文档（实现依据，必须严格遵守）】\n%s\n"
        % (requirement, sdd_md)
    )
    if extra_feedback:
        user += "\n【上一轮失败反馈（增量修复，不要重做无关内容）】\n%s\n" % extra_feedback
    user += (
        "\n请按 SDD 文档直接修改当前工作目录（即目标工程根）中的代码完成实现。\n"
        "完成后输出改动摘要（改了哪些文件、每处改动对应 SDD 的哪条契约/验收标准）。"
    )
    box = ToolBox(workdir)
    agent = LiteAgent(client, box, log=log)
    summary = agent.run(system, user)
    if box.written_files:
        summary += "\n[本 Agent 写过的文件: %s]" % ", ".join(box.written_files)
    return summary


# ================= 阶段4：编译（客观仲裁） =================
def run_compile(workdir: str, compile_cmd: str, timeout: int = 300):
    """真实编译子进程，退出码仲裁。返回 (ok, 输出, 错误摘录)。"""
    try:
        r = subprocess.run(
            compile_cmd, shell=True, cwd=workdir,
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        return False, "", "编译命令超时（>%ds）: %s" % (timeout, compile_cmd)
    output = (r.stdout or "") + (r.stderr or "")
    return r.returncode == 0, output, extract_errors(output)


def _smart_truncate_diff(diff: str, budget: int = 24000) -> str:
    """按文件分配额度截断 diff：确保每个文件的改动都能被评审看到，而非前几个文件占满额度。
    策略：按 'diff --git' 拆分为各文件块；若总量 ≤ 预算则全量返回；
    否则每文件分配 budget/文件数 的额度，尾部保留（新增行比删除行更重要）。"""
    if not diff or not diff.strip():
        return "(无改动)"
    # 按文件拆分
    parts = re.split(r"(?=^diff --git )", diff, flags=re.M)
    parts = [p for p in parts if p.strip()]
    if not parts:
        return diff[:budget]
    total = sum(len(p) for p in parts)
    if total <= budget:
        return diff
    # 按文件分配额度
    n = len(parts)
    per = max(2000, budget // n)  # 每个文件至少 2000 字符
    out = []
    for p in parts:
        if len(p) <= per:
            out.append(p)
        else:
            # 保留头部的 diff --git 行，然后取尾部（新增行更重要）
            head_end = p.index("\n", p.index("diff --git") + 10) + 1
            head = p[:head_end]
            tail = p[-(per - len(head)):]
            out.append(head + "...[中间截断]...\n" + tail)
    result = "".join(out)
    if len(result) > budget * 1.2:  # 仍超限则整体截断
        result = result[:budget]
    return result


def extract_errors(output: str) -> str:
    """错误提取双层策略：正则筛 error 行优先，否则尾部截取（模型看一眼就能用）。"""
    lines = output.splitlines()
    err_lines = [l for l in lines if re.search(r"\berror\b|\b错误\b", l, re.I)]
    if err_lines:
        return "\n".join(err_lines[:40])
    return "\n".join(lines[-60:]) if lines else "(无输出)"


# ================= 阶段5：规约对照评审（只读） =================
def run_review(client: LLMClient, sdd_md: str, diff: str,
               code_summary: str, log=lambda msg: None) -> dict:
    """只读评审：以 SDD 为验收依据裁决。返回 {pass: bool, issues: [..]}。"""
    system = load_standard("review.md")
    user = (
        "【SDD 设计文档（验收依据）】\n%s\n\n"
        "【编码 Agent 改动摘要】\n%s\n\n"
        "【目标工程 git diff】\n%s\n\n"
        "请对照 SDD 的接口契约与验收标准（Given/When/Then）逐条核查上述改动，"
        "并按系统提示中的 JSON 格式输出裁决。"
        % (sdd_md, code_summary, _smart_truncate_diff(diff, 24000))
    )
    log("评审 Agent：只读模式，以 SDD 为基准仲裁")
    text = client.complete([
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ])
    verdict = parse_verdict(text)
    log("评审结论：%s（%d 条意见）"
        % ("通过" if verdict["pass"] else "不通过", len(verdict["issues"])))
    return verdict


def parse_verdict(text: str) -> dict:
    """裁决 JSON 解析容错：剥围栏、字段同义词、解析失败默认判负。"""
    raw = _extract_json(text)
    if raw is None:
        return {"pass": False,
                "issues": ["评审输出无法解析为 JSON，默认判负。原文：%s" % text[:500]]}
    passed = raw.get("pass", raw.get("passed", raw.get("通过", False)))
    if isinstance(passed, str):
        passed = passed.strip().lower() in ("true", "yes", "通过", "pass")
    issues = raw.get("issues", raw.get("problems", raw.get("意见", [])))
    if isinstance(issues, str):
        issues = [issues]
    if not isinstance(issues, list):
        issues = [str(issues)]
    return {"pass": bool(passed), "issues": [str(i) for i in issues]}


def _extract_json(text: str):
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        # 容错：剥掉可能的代码围栏后再试
        t = re.sub(r"```(?:json)?", "", m.group(0))
        try:
            return json.loads(t)
        except Exception:
            return None
