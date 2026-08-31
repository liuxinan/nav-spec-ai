# -*- coding: utf-8 -*-
"""编码 Agent 受控工具箱：路径沙箱（越界拒绝）+ 命令超时 + 修改追踪。

工具白名单：read_file / write_file / edit_file / list_dir / run_command
所有文件路径解析后必须落在目标仓库内，否则拒绝执行。
"""
import os
import subprocess

MAX_READ = 60_000       # 单文件读取上限（字符）
MAX_LIST = 400          # 目录列表上限（条）
CMD_TIMEOUT = 180       # 命令超时（秒）

# 提供给 LLM 的工具 schema（OpenAI function calling 格式）
TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取目标工程内一个文本文件的内容",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对目标工程根的路径"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "整体写入一个文件（新建或覆盖）",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string", "description": "完整文件内容"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "精确文本替换：把文件中 old_text 的第一次出现替换为 new_text",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_text": {"type": "string", "description": "必须与文件内容逐字一致"},
                    "new_text": {"type": "string"},
                },
                "required": ["path", "old_text", "new_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "列出目录内容（相对目标工程根），忽略 node_modules/.git 等",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "默认为根目录 '.'"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "在目标工程根目录执行一条命令（如 dir、npm install）",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                },
                "required": ["command"],
            },
        },
    },
]

_IGNORE_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".idea"}


class ToolBox:
    """绑定目标仓库（靠 cwd 语义）的工具执行器。"""

    def __init__(self, workdir: str):
        self.workdir = os.path.realpath(workdir)
        self.written_files = []   # 修改感知：本 Agent 会话内写过的文件

    # ---------------- 沙箱路径校验 ----------------
    def _resolve(self, rel: str) -> str:
        p = os.path.realpath(os.path.join(self.workdir, rel))
        if p != self.workdir and not p.startswith(self.workdir + os.sep):
            raise ValueError("路径越界（仅允许目标工程内）: %s" % rel)
        return p

    # ---------------- 工具实现 ----------------
    def _read_file(self, path: str) -> str:
        p = self._resolve(path)
        if not os.path.isfile(p):
            return "错误: 文件不存在 %s" % path
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            content = f.read(MAX_READ)
        if len(content) >= MAX_READ:
            content += "\n...[已截断]"
        return content or "(空文件)"

    def _write_file(self, path: str, content: str) -> str:
        p = self._resolve(path)
        os.makedirs(os.path.dirname(p) or p, exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="") as f:
            f.write(content)
        self._track(p)
        return "已写入 %s（%d 字符）" % (path, len(content))

    def _edit_file(self, path: str, old_text: str, new_text: str) -> str:
        p = self._resolve(path)
        if not os.path.isfile(p):
            return "错误: 文件不存在 %s" % path
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        if old_text not in content:
            return "错误: old_text 在文件中未找到（请先 read_file 获取准确内容）"
        content = content.replace(old_text, new_text, 1)
        with open(p, "w", encoding="utf-8", newline="") as f:
            f.write(content)
        self._track(p)
        return "已修改 %s" % path

    def _list_dir(self, path: str = ".") -> str:
        p = self._resolve(path)
        if not os.path.isdir(p):
            return "错误: 目录不存在 %s" % path
        lines = []
        for base, dirs, files in os.walk(p):
            rel = os.path.relpath(base, self.workdir)
            depth = 0 if rel == "." else rel.count(os.sep) + 1
            if depth >= 2:  # 只扫两层，控制上下文量
                dirs[:] = []
                continue
            dirs[:] = sorted(d for d in dirs if d not in _IGNORE_DIRS)
            for name in sorted(files):
                lines.append(os.path.normpath(os.path.join(rel, name)))
                if len(lines) >= MAX_LIST:
                    lines.append("...[已达列表上限]")
                    return "\n".join(lines)
        return "\n".join(lines) or "(空目录)"

    def _run_command(self, command: str) -> str:
        try:
            r = subprocess.run(
                command, shell=True, cwd=self.workdir,
                capture_output=True, text=True, timeout=CMD_TIMEOUT,
                encoding="utf-8", errors="replace",
            )
            out = (r.stdout or "") + (r.stderr or "")
            if len(out) > 8000:
                out = out[:4000] + "\n...[截断]...\n" + out[-4000:]
            return "退出码 %d\n%s" % (r.returncode, out.strip())
        except subprocess.TimeoutExpired:
            return "错误: 命令超时（>%ds）: %s" % (CMD_TIMEOUT, command)

    def _track(self, abs_path: str):
        rel = os.path.relpath(abs_path, self.workdir)
        if rel not in self.written_files:
            self.written_files.append(rel)

    # ---------------- 统一入口（白名单路由） ----------------
    def execute(self, name: str, args: dict) -> str:
        try:
            if name == "read_file":
                return self._read_file(args["path"])
            if name == "write_file":
                return self._write_file(args["path"], args.get("content", ""))
            if name == "edit_file":
                return self._edit_file(args["path"], args["old_text"], args["new_text"])
            if name == "list_dir":
                return self._list_dir(args.get("path", "."))
            if name == "run_command":
                return self._run_command(args["command"])
            return "错误: 未知工具 %s（白名单外）" % name
        except ValueError as e:
            return "错误: %s" % e
        except KeyError as e:
            return "错误: 缺少参数 %s" % e
        except Exception as e:
            return "错误: %s" % e
