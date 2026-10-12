#!/usr/bin/env python3
"""Local launcher console for the read-only forecast dashboard.

This process only manages a child ``live_dashboard_server.py`` started by the
same launcher instance.  It never places orders and refuses to start when the
requested dashboard port is already occupied.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import http.client
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


SOURCE_ROOT = Path(__file__).resolve().parent
if getattr(sys, "frozen", False):
    _executable_dir = Path(sys.executable).resolve().parent
    _root_candidates = [_executable_dir, *_executable_dir.parents]
    PROJECT_ROOT = next(
        (candidate for candidate in _root_candidates if (candidate / "live_dashboard_server.py").is_file()),
        _executable_dir,
    )
else:
    PROJECT_ROOT = SOURCE_ROOT
RESOURCE_ROOT = Path(getattr(sys, "_MEIPASS", SOURCE_ROOT))
DEFAULT_DASHBOARD_PORT = 8765
DEFAULT_LAUNCHER_PORT = 8760
SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,180}$")


def default_python_executable() -> str:
    """Return a Python interpreter for the managed backend.

    A frozen launcher must not use ``sys.executable`` here: that path is the
    launcher EXE itself, not a Python interpreter, and would make the child
    backend exit immediately after the console appears to start normally.
    """
    configured = os.environ.get("AIPRP_PYTHON")
    if configured:
        return configured
    if not getattr(sys, "frozen", False):
        return sys.executable
    for candidate in ("python", "python3", "py"):
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    return "python"


def port_available(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


class LauncherState:
    def __init__(self, python_executable: str, default_dashboard_port: int) -> None:
        self.python_executable = python_executable
        self.default_dashboard_port = default_dashboard_port
        self.lock = threading.RLock()
        self.process: subprocess.Popen[Any] | None = None
        self.last_config: dict[str, Any] = {}
        self.last_error: str | None = None
        self.output_tail: list[str] = []
        self.lifecycle = "not_started"

    def _healthy(self) -> bool:
        connection = http.client.HTTPConnection("127.0.0.1", self.last_config.get("port", self.default_dashboard_port), timeout=1)
        try:
            connection.request("GET", "/api/health")
            response = connection.getresponse()
            payload = json.loads(response.read(65536))
            return (response.status == 200 and payload.get("ok") is True
                    and payload.get("service") == "live_dashboard_server"
                    and payload.get("do_not_auto_trade") is True)
        except (OSError, ValueError, http.client.HTTPException, AttributeError):
            return False
        finally:
            connection.close()

    def _running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def status(self) -> dict[str, Any]:
        with self.lock:
            running = self._running()
            returncode = self.process.poll() if self.process is not None else None
            if returncode is not None:
                self.lifecycle = "failed"
                detail = "".join(self.output_tail[-12:]).strip()
                self.last_error = f"dashboard process exited with code {returncode}"
                if detail:
                    self.last_error += f": {detail[-1800:]}"
            ready = running and self._healthy()
            if running:
                self.lifecycle = "ready" if ready else "starting"
            return {
                "ok": True,
                "launcher": "dashboard_launcher",
                "dashboard_running": running,
                "dashboard_ready": ready,
                "state": self.lifecycle,
                "exit_code": returncode,
                "project_root": str(PROJECT_ROOT),
                "backend_entry": str(PROJECT_ROOT / "live_dashboard_server.py"),
                "dashboard_pid": self.process.pid if running and self.process else None,
                "dashboard_port": self.last_config.get("port", self.default_dashboard_port),
                "dashboard_url": (
                    f"http://127.0.0.1:{self.last_config.get('port', self.default_dashboard_port)}/"
                    if ready else None
                ),
                "mode": self.last_config.get("mode"),
                "config": dict(self.last_config),
                "last_error": self.last_error,
                "output_tail": list(self.output_tail[-12:]),
                "do_not_auto_trade": True,
            }

    @staticmethod
    def _clean_config(payload: dict[str, Any], default_port: int) -> dict[str, Any]:
        mode = str(payload.get("mode") or "dynamic").strip().lower()
        if mode not in {"dynamic", "fixed", "backtest"}:
            raise ValueError("mode must be dynamic, fixed or backtest")
        slug = str(payload.get("slug") or "").strip()
        if mode == "fixed" and (not slug or not SLUG_RE.fullmatch(slug)):
            raise ValueError("fixed mode requires a safe slug")
        extra_slugs = [str(value).strip() for value in (payload.get("extra_slugs") or [])]
        if any(not SLUG_RE.fullmatch(value) for value in extra_slugs if value):
            raise ValueError("extra slug contains unsupported characters")
        db = str(payload.get("db") or "data\\extended.sqlite").strip()
        model = str(payload.get("model") or "data\\extended_model_v4.json").strip()
        proxy = str(payload.get("proxy") or "http://127.0.0.1:7897").strip()
        if not db or not model:
            raise ValueError("db and model are required")
        port = int(payload.get("port") or default_port)
        refresh = int(payload.get("refresh_seconds") or 30)
        research_refresh = int(payload.get("research_refresh_seconds") or 30)
        raw_timeout = payload.get("timeout", 20)
        if isinstance(raw_timeout, bool) or not re.fullmatch(r"\d+", str(raw_timeout)):
            raise ValueError("timeout must be an integer between 1 and 300")
        timeout = int(raw_timeout)
        if not 1024 <= port <= 65535:
            raise ValueError("dashboard port must be between 1024 and 65535")
        if refresh < 10 or refresh > 86400:
            raise ValueError("refresh_seconds must be between 10 and 86400")
        if research_refresh < 30 or research_refresh > 86400:
            raise ValueError("research_refresh_seconds must be between 30 and 86400")
        if timeout < 1 or timeout > 300:
            raise ValueError("timeout must be between 1 and 300")
        return {
            "mode": mode,
            "slug": slug,
            "extra_slugs": [value for value in extra_slugs if value],
            "db": db,
            "model": model,
            "proxy": proxy,
            "port": port,
            "refresh_seconds": refresh,
            "research_refresh_seconds": research_refresh,
            "timeout": timeout,
        }

    def start(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            return self._start(payload)
        except Exception as exc:
            with self.lock:
                if not self._running():
                    self.lifecycle = "failed"
                    self.last_error = f"{type(exc).__name__}: {exc}"
            raise

    def _start(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            if self._running():
                raise RuntimeError("dashboard service is already running")
            config = self._clean_config(payload, self.default_dashboard_port)
            if not port_available(config["port"]):
                raise RuntimeError(f"dashboard port {config['port']} is already listening")
            db_path = (PROJECT_ROOT / config["db"]).resolve() if not os.path.isabs(config["db"]) else Path(config["db"])
            model_path = (PROJECT_ROOT / config["model"]).resolve() if not os.path.isabs(config["model"]) else Path(config["model"])
            if not db_path.is_file():
                raise FileNotFoundError(f"database not found: {db_path}")
            if not model_path.is_file():
                raise FileNotFoundError(f"model not found: {model_path}")
            args = [
                self.python_executable,
                str(PROJECT_ROOT / "live_dashboard_server.py"),
                "--db", str(db_path),
                "--model", str(model_path),
                "--proxy", config["proxy"],
                "--port", str(config["port"]),
                "--refresh-seconds", str(config["refresh_seconds"]),
                "--research-refresh-seconds", str(config["research_refresh_seconds"]),
                "--timeout", str(config["timeout"]),
            ]
            if config["mode"] == "dynamic":
                args.append("--dynamic-discovery")
            elif config["mode"] == "fixed":
                args.extend(["--slug", config["slug"]])
            else:
                args.append("--no-background")
            for extra in config["extra_slugs"]:
                args.extend(["--extra-slug", extra])
            self.process = subprocess.Popen(
                args,
                cwd=str(PROJECT_ROOT),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            self.last_config = config
            self.last_error = None
            self.output_tail = []
            self.lifecycle = "starting"
            threading.Thread(target=self._capture_output, args=(self.process,), daemon=True).start()
            return self.status()

    def _capture_output(self, process: subprocess.Popen[Any]) -> None:
        stream = process.stdout
        if stream is None:
            return
        for line in stream:
            with self.lock:
                if self.process is not process:
                    return
                self.output_tail.append(str(line))
                self.output_tail = self.output_tail[-40:]

    def stop(self) -> dict[str, Any]:
        with self.lock:
            if self.process is not None and self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=3)
            self.process = None
            self.lifecycle = "stopped"
            self.last_error = None
            return self.status()

    def restart(self) -> dict[str, Any]:
        with self.lock:
            if not self._running():
                raise RuntimeError("dashboard service is not running")
            config = dict(self.last_config)
            self.stop()
            return self.start(config)


class LauncherHandler(BaseHTTPRequestHandler):
    state: LauncherState
    static_file: Path

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length > 64 * 1024:
            raise ValueError("request too large")
        raw = self.rfile.read(length) if length else b"{}"
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("JSON object required")
        return payload

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/api/status":
            self._json(200, self.state.status())
            return
        if self.path in {"/", "/index.html"}:
            body = self.static_file.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._json(404, {"ok": False, "message": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        try:
            payload = self._body()
            if self.path == "/api/start":
                result = self.state.start(payload)
            elif self.path == "/api/stop":
                result = self.state.stop()
            elif self.path == "/api/restart":
                result = self.state.restart()
            elif self.path == "/api/open-dashboard":
                status = self.state.status()
                if not status["dashboard_ready"]:
                    raise RuntimeError(status.get("last_error") or "研究台尚未就绪，请先启动并查看状态和日志")
                webbrowser.open(status["dashboard_url"])
                result = status
            else:
                self._json(404, {"ok": False, "message": "not found"})
                return
            self._json(200, result)
        except Exception as exc:
            self._json(400, {"ok": False, "message": f"{type(exc).__name__}: {exc}", "do_not_auto_trade": True})

    def log_message(self, format: str, *args: Any) -> None:
        return


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Launch the local forecast dashboard control console.")
    parser.add_argument("--launcher-port", type=int, default=DEFAULT_LAUNCHER_PORT)
    parser.add_argument("--dashboard-port", type=int, default=DEFAULT_DASHBOARD_PORT)
    parser.add_argument("--python", default=default_python_executable())
    parser.add_argument("--no-browser", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if not 1024 <= args.launcher_port <= 65535:
        raise SystemExit("launcher port must be between 1024 and 65535")
    state = LauncherState(args.python, args.dashboard_port)
    handler = type("ConfiguredLauncherHandler", (LauncherHandler,), {
        "state": state,
        "static_file": RESOURCE_ROOT / "dashboard_launcher.html",
    })
    server = ThreadingHTTPServer(("127.0.0.1", args.launcher_port), handler)
    url = f"http://127.0.0.1:{args.launcher_port}/"
    print(f"Launcher console: {url}", flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 130
    finally:
        state.stop()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
