# -*- coding: utf-8 -*-
"""OpenAI 兼容 LLM 客户端：urllib 直连，零第三方依赖。

- 普通 chat：用于 DDD/SDD 生成、评审裁决
- 工具调用：用于编码 Agent 循环；若服务端不支持 function calling，
  探测一次后自动降级为"文本协议"模式（由 agent.py 处理）。
"""
import base64
import json
import urllib.parse
import urllib.request
import urllib.error


class LLMError(Exception):
    pass


class LLMClient:
    def __init__(self, llm_cfg: dict):
        self.base_url = llm_cfg["baseUrl"].rstrip("/")
        self.api_key = llm_cfg["apiKey"]
        self.model = llm_cfg["model"]
        self.temperature = llm_cfg.get("temperature", 0.3)
        self.proxy = (llm_cfg.get("proxy") or "").strip()  # 如 http://user:pass@host:port
        self._tools_ok = None  # None=未探测

    # ---------------- 基础请求 ----------------
    def _post(self, payload: dict, timeout: int = 300) -> dict:
        url = self.base_url + "/chat/completions"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer %s" % self.api_key,
            },
        )
        try:
            # baseUrl 指向本机时绕过一切代理（避免 Windows 系统代理干扰）
            host = urllib.parse.urlparse(self.base_url).hostname or ""
            if host in ("localhost", "127.0.0.1", "::1"):
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                resp = opener.open(req, timeout=timeout)
            elif self.proxy:
                # 显式配置的 HTTP 代理（支持 Basic 认证：http://user:pass@host:port）
                u = urllib.parse.urlparse(self.proxy)
                if u.username is not None:
                    cred = "%s:%s" % (urllib.parse.unquote(u.username),
                                      urllib.parse.unquote(u.password or ""))
                    req.add_header("Proxy-Authorization",
                                   "Basic %s" % base64.b64encode(cred.encode()).decode())
                    # 去掉认证信息后的代理地址（部分代理实现不接受带 userinfo 的 URL）
                    proxy_url = urllib.parse.urlunparse(
                        u._replace(netloc="%s:%s" % (u.hostname, u.port or 8080)))
                else:
                    proxy_url = self.proxy
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({
                    "http": proxy_url, "https": proxy_url,
                }))
                resp = opener.open(req, timeout=timeout)
            else:
                resp = urllib.request.urlopen(req, timeout=timeout)
            with resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "ignore")[:2000]
            except Exception:
                pass
            raise LLMError("LLM HTTP %s: %s" % (e.code, body))
        except Exception as e:
            raise LLMError("LLM 网络错误: %s" % e)

    # ---------------- 普通补全（返回文本） ----------------
    def complete(self, messages: list) -> str:
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
        }
        resp = self._post(payload)
        try:
            return resp["choices"][0]["message"]["content"] or ""
        except Exception:
            raise LLMError("LLM 响应结构异常: %s" % json.dumps(resp, ensure_ascii=False)[:500])

    # ---------------- 工具调用（返回完整响应） ----------------
    def chat_with_tools(self, messages: list, tools: list) -> dict:
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "tools": tools,
        }
        return self._post(payload)

    # ---------------- 工具支持探测（结果缓存） ----------------
    def supports_tools(self) -> bool:
        if self._tools_ok is not None:
            return self._tools_ok
        probe_tools = [{
            "type": "function",
            "function": {
                "name": "noop",
                "description": "探测用，不会被调用",
                "parameters": {"type": "object", "properties": {}},
            },
        }]
        try:
            self.chat_with_tools(
                [{"role": "user", "content": "hi"}], probe_tools)
            self._tools_ok = True
        except LLMError:
            self._tools_ok = False
        return self._tools_ok
