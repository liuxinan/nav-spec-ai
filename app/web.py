# -*- coding: utf-8 -*-
"""Web 层：纯标准库 http.server（ThreadingHTTPServer），前后端零构建链。

路由：
  GET  /                     前端单页（web/index.html）
  GET  /api/config           前端展示用配置（不回传 apiKey）
  POST /api/runs             提交一句话需求，创建运行
  GET  /api/runs             运行列表
  GET  /api/runs/<id>        运行状态快照（前端轮询）
  POST /api/runs/<id>/gate   人工确认 / 驳回（驳回需意见）
  POST /api/runs/<id>/resume 从失败处恢复（跳过 DDD/SDD，重试编码→编译→评审）
"""
import json
import os
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .pipeline import Pipeline

_WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")


def make_handler(pipeline: Pipeline, cfg: dict):
    class Handler(BaseHTTPRequestHandler):
        # ---------- 基础 ----------
        def _json(self, obj, code=200):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            try:
                return json.loads(self.rfile.read(n).decode("utf-8"))
            except Exception:
                return {}

        def log_message(self, fmt, *args):
            print("[http] " + fmt % args, flush=True)

        # ---------- GET ----------
        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/" or path == "/index.html":
                self._serve_file("index.html", "text/html; charset=utf-8")
            elif path == "/api/config":
                self._json({
                    "workdir": cfg["target"]["workdir"],
                    "compileCmd": cfg["target"]["compileCmd"],
                    "model": cfg["llm"]["model"],
                    "limits": cfg["limits"],
                })
            elif path == "/api/runs":
                self._json(pipeline.list_runs())
            elif path.startswith("/api/runs/"):
                run = pipeline.get_run(path.split("/")[3])
                if not run:
                    self._json({"error": "运行不存在"}, 404)
                else:
                    self._json(run.snapshot())
            else:
                self._json({"error": "未找到路由"}, 404)

        # ---------- POST ----------
        def do_POST(self):
            path = self.path.split("?")[0]
            try:
                if path == "/api/runs":
                    req = self._body()
                    run = pipeline.create_run(req.get("requirement", ""))
                    self._json({"id": run.id}, 201)
                elif path.endswith("/gate"):
                    run_id = path.split("/")[3]
                    req = self._body()
                    pipeline.submit_gate(
                        run_id, req.get("action", ""),
                        req.get("content", ""), req.get("feedback", ""))
                    self._json({"ok": True})
                elif path.endswith("/resume"):
                    run_id = path.split("/")[3]
                    run = pipeline.resume_run(run_id)
                    self._json({"id": run.id})
                else:
                    self._json({"error": "未找到路由"}, 404)
            except ValueError as e:
                self._json({"error": str(e)}, 400)
            except Exception as e:
                traceback.print_exc()
                self._json({"error": "服务器内部错误: %s" % e}, 500)

        # ---------- 静态文件 ----------
        def _serve_file(self, name: str, mime: str):
            p = os.path.join(_WEB_DIR, name)
            if not os.path.isfile(p):
                self._json({"error": "文件不存在"}, 404)
                return
            with open(p, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def serve(cfg: dict, pipeline: Pipeline):
    host = cfg["server"]["host"]
    port = int(cfg["server"]["port"])
    httpd = ThreadingHTTPServer((host, port), make_handler(pipeline, cfg))
    print("=" * 56, flush=True)
    print("NavSpec AI 需求工程流水线已启动", flush=True)
    print("  页面   : http://%s:%d/" % (host, port), flush=True)
    print("  目标工程: %s" % cfg["target"]["workdir"], flush=True)
    print("  编译命令: %s" % cfg["target"]["compileCmd"], flush=True)
    print("  LLM    : %s / %s" % (cfg["llm"]["baseUrl"], cfg["llm"]["model"]), flush=True)
    print("=" * 56, flush=True)
    httpd.serve_forever()
