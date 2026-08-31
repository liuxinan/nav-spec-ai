# -*- coding: utf-8 -*-
"""配置加载：config.json 缺失时给出清晰指引（失败要早）。"""
import json
import os

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(_ROOT, "config.json")

DEFAULTS = {
    "llm": {
        "baseUrl": "",        # OpenAI 兼容地址，如 https://api.example.com/v1
        "apiKey": "",
        "model": "",
        "temperature": 0.3,
        "proxy": "",          # HTTP 代理，支持认证，如 http://user:pass@192.168.2.78:8080；留空不走代理
    },
    "target": {
        "workdir": "",        # 目标演示工程路径（必须是 git 仓库）
        "compileCmd": "",     # 编译/验证命令，如 npm run build / python -m compileall -q .
    },
    "server": {"host": "127.0.0.1", "port": 8000},
    # 有界重试：现场演示设小值，缩短时长
    "limits": {
        "compileRetries": 2,   # 编译失败 → 回灌重编码的上限
        "reviewRetries": 1,    # 评审判负 → 回灌重编码的上限
        "regenLimit": 2,       # 人工驳回文档 → 重新生成的上限
    },
}


class ConfigError(Exception):
    pass


def load_config() -> dict:
    """读取 config.json 并与默认值合并；关键字段缺失立即报错（退出码 3 语义）。"""
    cfg = json.loads(json.dumps(DEFAULTS))  # deep copy
    if not os.path.exists(CONFIG_PATH):
        raise ConfigError("未找到 config.json，请复制 config.example.json 并填写后重试")
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        user = json.load(f)
    for k, v in user.items():
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            cfg[k].update(v)
        else:
            cfg[k] = v
    # 入口校验：LLM 与目标工程是硬前提
    if not cfg["llm"]["baseUrl"] or not cfg["llm"]["apiKey"] or not cfg["llm"]["model"]:
        raise ConfigError("config.json 中 llm.baseUrl / apiKey / model 未填写完整")
    if not cfg["target"]["workdir"]:
        raise ConfigError("config.json 中 target.workdir 未填写（目标演示工程路径）")
    if not cfg["target"]["compileCmd"]:
        raise ConfigError("config.json 中 target.compileCmd 未填写（编译/验证命令）")
    wd = cfg["target"]["workdir"]
    if not os.path.isdir(wd):
        raise ConfigError("目标工程目录不存在: %s" % wd)
    if not os.path.isdir(os.path.join(wd, ".git")):
        raise ConfigError("目标工程不是 git 仓库（需要用 git diff 感知修改）: %s" % wd)
    return cfg


def root() -> str:
    return _ROOT
