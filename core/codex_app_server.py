"""Small typed boundary around the local Codex app-server JSON-RPC API."""

from __future__ import annotations

import json
import selectors
import subprocess
import time
from types import TracebackType
from typing import Any, Callable

from core.codex_fallback import resolve_codex_bin


DEFAULT_TIMEOUT_SECONDS = 8


class CodexAppServerError(RuntimeError):
    """The local Codex app-server did not complete a bounded request."""


class CodexAppServerClient:
    """One short-lived, sequential app-server stdio session.

    Jarvis uses only documented protocol methods. It never reads Codex rollout
    files or the internal state database directly.
    """

    def __init__(
        self,
        binary: str = "",
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        client_name: str = "jarvis",
        client_version: str = "0.1.0",
        experimental_api: bool = True,
        popen_factory: Callable[..., Any] = subprocess.Popen,
    ) -> None:
        self.binary = str(binary or "")
        self.timeout = max(0.1, float(timeout))
        self.client_name = str(client_name or "jarvis")
        self.client_version = str(client_version or "0.1.0")
        self.experimental_api = bool(experimental_api)
        self.popen_factory = popen_factory
        self.process: Any = None
        self.user_agent = ""
        self._next_id = 1

    def __enter__(self) -> "CodexAppServerClient":
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def start(self) -> None:
        if self.process is not None:
            return
        executable = self.binary or resolve_codex_bin()
        if not executable:
            raise CodexAppServerError("Codex CLI is unavailable")
        try:
            self.process = self.popen_factory(
                [executable, "app-server", "--stdio"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
            initialized = self.request(
                "initialize",
                {
                    "clientInfo": {
                        "name": self.client_name,
                        "version": self.client_version,
                    },
                    "capabilities": {
                        "experimentalApi": self.experimental_api,
                    },
                },
            )
            if not isinstance(initialized, dict):
                raise CodexAppServerError(
                    "Codex app-server returned an invalid initialize result"
                )
            self.user_agent = str(initialized.get("userAgent") or "")
            self.notify("initialized", {})
        except (BrokenPipeError, OSError) as exc:
            self.close()
            raise CodexAppServerError(
                "Codex app-server could not be started"
            ) from exc
        except Exception:
            self.close()
            raise

    def request(
        self, method: str, params: dict[str, Any] | None = None, *,
        timeout: float | None = None,
    ) -> Any:
        if self.process is None or self.process.stdin is None:
            raise CodexAppServerError("Codex app-server is not running")
        request_id = self._next_id
        self._next_id += 1
        self._write({
            "id": request_id,
            "method": str(method),
            "params": params or {},
        })
        return self._read_response(
            request_id,
            method=str(method),
            timeout=self.timeout if timeout is None else float(timeout),
        )

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self._write({"method": str(method), "params": params or {}})

    def _write(self, payload: dict[str, Any]) -> None:
        if self.process is None or self.process.stdin is None:
            raise CodexAppServerError("Codex app-server is not running")
        try:
            self.process.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
            self.process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise CodexAppServerError(
                "Codex app-server connection closed unexpectedly"
            ) from exc

    def _read_response(
        self, request_id: int, *, method: str, timeout: float,
    ) -> Any:
        if self.process is None or self.process.stdout is None:
            raise CodexAppServerError("Codex app-server is not running")
        selector = selectors.DefaultSelector()
        selector.register(self.process.stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + max(0.1, timeout)
        try:
            while time.monotonic() < deadline:
                events = selector.select(max(0.0, deadline - time.monotonic()))
                if not events:
                    break
                line = self.process.stdout.readline()
                if not line:
                    break
                try:
                    payload = json.loads(line)
                except (json.JSONDecodeError, TypeError, ValueError):
                    continue
                if payload.get("id") != request_id:
                    continue
                error = payload.get("error")
                if error:
                    code = error.get("code") if isinstance(error, dict) else "unknown"
                    raise CodexAppServerError(
                        f"Codex app-server rejected {method} (code={code})"
                    )
                return payload.get("result")
        finally:
            selector.close()
        raise CodexAppServerError(f"Codex app-server timed out during {method}")

    def close(self) -> None:
        process = self.process
        self.process = None
        if process is None:
            return
        try:
            process.terminate()
            process.wait(timeout=1)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
                process.wait(timeout=1)
            except OSError:
                pass
            except subprocess.TimeoutExpired:
                pass
