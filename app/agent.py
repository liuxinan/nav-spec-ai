# -*- coding: utf-8 -*-
"""LiteAgent：自研轻量编码 Agent（纯标准库）。

两种模式，自动选择：
1. 工具模式：服务端支持 function calling 时，标准工具调用循环；
2. 文本协议模式（降级）：单轮请求，要求模型以 ```lang
   # file: 相对路径
   ``` 围栏输出完整文件内容，解析后写盘。

有界：工具模式最多 40 轮工具调用，必终止。
"""
import json
import re

from .llm import LLMClient, LLMError
from .tools import ToolBox, TOOL_SCHEMAS

MAX_TURNS = 40
# 文本协议模式的文件围栏：```任意语言\n# file: path\n...```
_FENCE_RE = re.compile(r"```[^\n]*\n[ \t]*(?:#|//|<!--)[ \t]*file[ \t]*[:：][ \t]*(\S+)\n(.*?)```", re.S)


def _strip_fence(text: str) -> str:
    """剥掉 markdown 代码围栏（部分模型习惯性包裹）。"""
    m = re.search(r"```(?:\w*)\n(.*)```", text, re.S)
    return m.group(1).strip() if m else text.strip()


def _parse_tool_args(raw) -> dict:
    """工具参数解析容错：接受 dict / JSON 字符串。"""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            return json.loads(_strip_fence(raw))
        except Exception:
            return {}
    return {}


class LiteAgent:
    def __init__(self, client: LLMClient, toolbox: ToolBox, log=lambda msg: None):
        self.client = client
        self.box = toolbox
        self.log = log  # 事件回调（写流水线日志）

    # ---------------- 主入口 ----------------
    def run(self, system_prompt: str, user_prompt: str) -> str:
        """驱动 Agent 改完文件，返回最终文本摘要。"""
        if self.client.supports_tools():
            self.log("编码 Agent：工具调用模式（function calling）")
            return self._run_tool_loop(system_prompt, user_prompt)
        self.log("编码 Agent：文本协议模式（服务端不支持 function calling，已降级）")
        return self._run_text_protocol(system_prompt, user_prompt)

    # ---------------- 工具模式：标准 function calling 循环 ----------------
    def _run_tool_loop(self, system_prompt: str, user_prompt: str) -> str:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        final_text = ""
        for turn in range(MAX_TURNS):
            resp = self.client.chat_with_tools(messages, TOOL_SCHEMAS)
            try:
                msg = resp["choices"][0]["message"]
            except Exception:
                raise LLMError("LLM 响应结构异常（工具模式）")
            content = msg.get("content") or ""
            tool_calls = msg.get("tool_calls") or []
            if content:
                final_text = content
            if not tool_calls:
                return final_text or "(Agent 未输出总结)"
            messages.append(msg)  # 保留 tool_calls 语义
            for tc in tool_calls:
                fn = (tc.get("function") or {})
                name = fn.get("name", "")
                args = _parse_tool_args(fn.get("arguments"))
                self.log("工具调用 %s(%s)" % (name, ", ".join(args.keys())))
                result = self.box.execute(name, args)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id", ""),
                    "content": str(result)[:8000],
                })
        return final_text or "已达到工具调用轮次上限（%d），强制收尾" % MAX_TURNS

    # ---------------- 文本协议模式（降级） ----------------
    def _run_text_protocol(self, system_prompt: str, user_prompt: str) -> str:
        protocol = (
            "\n\n【输出协议（必须严格遵守）】\n"
            "你需要修改的每个文件，都以如下围栏完整输出（整文件内容，不要省略）：\n"
            "```<语言>\n# file: <相对路径>\n<完整文件内容>\n```\n"
            "围栏之外可以写简短说明。未修改的文件不要输出。\n"
            "开始前可先输出你的改动计划。"
        )
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt + protocol},
        ]
        text = self.client.complete(messages)
        count = 0
        for m in _FENCE_RE.finditer(text):
            path, content = m.group(1).strip(), m.group(2)
            content = content.rstrip("\n") + "\n"
            result = self.box.execute("write_file", {"path": path, "content": content})
            self.log("写入文件 %s" % path)
            if not result.startswith("错误"):
                count += 1
        summary = text.split("```")[0].strip()  # 围栏前的说明部分
        if count == 0:
            summary = (summary or "") + "\n（警告：未能从输出中解析出任何文件）"
        return summary or "已写入 %d 个文件（文本协议模式）" % count
