from __future__ import annotations

import os
import time
from typing import Any, Optional

import httpx


class NuminousError(RuntimeError):
    """API error carrying the typed cause.

    err.cause is one of: user_image_build_failed, user_oom, user_timeout,
    provider_capacity, provider_infra, policy_killed_ttl, policy_killed_quota,
    auth, state, not_found.
    """

    def __init__(self, cause: str, message: str, status: int):
        super().__init__(f"[{cause}] {message}")
        self.cause = cause
        self.message = message
        self.status = status

    @property
    def is_provider_fault(self) -> bool:
        return self.cause.startswith("provider_")

    @property
    def retryable(self) -> bool:
        # capacity clears (or use reservations); infra may clear; user_* will not.
        return self.cause.startswith("provider_")


class _Resource:
    def __init__(self, c: "Numinous"):
        self._c = c


class Templates(_Resource):
    def pack(self, name: str, *, image: str | None = None,
             dockerfile: str | None = None, context: str | None = None,
             warm_cmd: str | None = None) -> dict:
        source: dict[str, Any] = {}
        if image:
            source = {"type": "image", "image": image, "warm_cmd": warm_cmd}
        elif dockerfile:
            source = {"type": "dockerfile", "dockerfile": dockerfile,
                      "context": context or ".", "warm_cmd": warm_cmd}
        return self._c._post("/v1/templates", {"name": name, "source": source},
                             timeout=1800)

    def list(self) -> list[dict]:
        return self._c._get("/v1/templates")

    def get(self, template_id: str) -> dict:
        return self._c._get(f"/v1/templates/{template_id}")


class Roms(_Resource):
    def create(self, name: str, files: dict[str, str]) -> dict:
        return self._c._post("/v1/roms", {"name": name, "files": files})


class Sandboxes(_Resource):
    def create(self, *, template_id: str | None = None, image: str | None = None,
               rom_id: str | None = None, vcpu: int = 2, mem_gib: float = 4.0,
               ttl_seconds: int = 0, launch_token: str | None = None,
               labels: dict[str, str] | None = None,
               egress: str = "allow", allow: list[str] | None = None,
               env: dict[str, str] | None = None,
               auto_suspend_idle_seconds: int = 0,
               volumes: list[dict] | None = None) -> dict:
        return self._c._post("/v1/sandboxes", {
            "template_id": template_id, "image": image, "rom_id": rom_id,
            "vcpu": vcpu, "mem_gib": mem_gib, "ttl_seconds": ttl_seconds,
            "launch_token": launch_token, "labels": labels or {},
            "network": {"egress": egress, "allow": allow or []},
            "env": env or {},
            "auto_suspend_idle_seconds": auto_suspend_idle_seconds,
            "volumes": volumes or [],
        }, timeout=600)

    def get(self, sandbox_id: str) -> dict:
        return self._c._get(f"/v1/sandboxes/{sandbox_id}")

    def list(self, *, label: str | None = None, state: str | None = None) -> list[dict]:
        params = {}
        if label:
            params["label"] = label
        if state:
            params["state"] = state
        return self._c._get("/v1/sandboxes", params=params)

    def exec(self, sandbox_id: str, command: str, timeout_sec: float = 300,
             *, cwd: str | None = None, env: dict[str, str] | None = None,
             user: str | None = None) -> dict:
        return self._c._post(f"/v1/sandboxes/{sandbox_id}/exec",
                             {"command": command, "timeout_sec": timeout_sec,
                              "cwd": cwd, "env": env or {}, "user": user},
                             timeout=timeout_sec + 30)

    def logs(self, sandbox_id: str, tail: int = 500) -> str:
        return self._c._get(f"/v1/sandboxes/{sandbox_id}/logs",
                            params={"tail": tail})["logs"]

    def suspend(self, sandbox_id: str) -> dict:
        return self._c._post(f"/v1/sandboxes/{sandbox_id}/suspend", {})

    def resume(self, sandbox_id: str) -> dict:
        return self._c._post(f"/v1/sandboxes/{sandbox_id}/resume", {})

    def export(self, sandbox_id: str, path: str, to: str | None = None) -> dict:
        """Works during the run and after the sandbox terminated."""
        return self._c._post(f"/v1/sandboxes/{sandbox_id}/export",
                             {"path": path, "to": to}, timeout=1800)

    def destroy(self, sandbox_id: str) -> dict:
        """Returns the sandbox with teardown_proof attached."""
        return self._c._delete(f"/v1/sandboxes/{sandbox_id}")


    def put_file(self, sandbox_id: str, path: str, data: bytes) -> dict:
        import base64
        return self._c._put(f"/v1/sandboxes/{sandbox_id}/files",
                            {"path": path,
                             "content_b64": base64.b64encode(data).decode()})

    def get_file(self, sandbox_id: str, path: str) -> bytes:
        import base64
        out = self._c._get(f"/v1/sandboxes/{sandbox_id}/files",
                           params={"path": path})
        return base64.b64decode(out["content_b64"])

    def checkpoint(self, sandbox_id: str, name: str | None = None) -> dict:
        """Freeze a running sandbox into a new template (fork point)."""
        return self._c._post(f"/v1/sandboxes/{sandbox_id}/checkpoint",
                             {"name": name}, timeout=900)

    def metrics(self, sandbox_id: str) -> dict:
        """Observed cpu/mem samples while running."""
        return self._c._get(f"/v1/sandboxes/{sandbox_id}/metrics")

    def events(self, sandbox_id: str) -> list[dict]:
        return self._c._get(f"/v1/sandboxes/{sandbox_id}/events")

    def wait(self, sandbox_id: str, *, until: str = "terminated",
             timeout: float = 600, poll: float = 2.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            sb = self.get(sandbox_id)
            if sb["state"] in (until, "failed", "terminated"):
                return sb
            time.sleep(poll)
        raise TimeoutError(f"{sandbox_id} not {until} after {timeout}s")


class Volumes(_Resource):
    def create(self, name: str, size_gib: float = 10.0) -> dict:
        """Idempotent by name within your org; safe to call on every start."""
        return self._c._post("/v1/volumes", {"name": name, "size_gib": size_gib})

    def list(self) -> list[dict]:
        return self._c._get("/v1/volumes")

    def delete(self, volume_id: str) -> dict:
        return self._c._delete(f"/v1/volumes/{volume_id}")


class Usage(_Resource):
    def query(self, *, label: str | None = None) -> dict:
        params = {"label": label} if label else {}
        return self._c._get("/v1/usage", params=params)


class Capacity(_Resource):
    def get(self) -> dict:
        return self._c._get("/v1/capacity")

    def reserve(self, *, vcpu: int, mem_gib: float, count: int,
                duration_minutes: int = 120,
                labels: dict[str, str] | None = None) -> dict:
        return self._c._post("/v1/reservations", {
            "vcpu": vcpu, "mem_gib": mem_gib, "count": count,
            "duration_minutes": duration_minutes, "labels": labels or {}})


class Numinous:
    def __init__(self, api_url: str | None = None, api_key: str | None = None):
        self.api_url = (api_url or os.environ.get(
            "NUMINOUS_API_URL", "http://127.0.0.1:8400")).rstrip("/")
        self.api_key = api_key or os.environ.get("NUMINOUS_API_KEY", "nk_local_dev")
        self._http = httpx.Client(
            base_url=self.api_url,
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=60,
        )
        self.templates = Templates(self)
        self.roms = Roms(self)
        self.sandboxes = Sandboxes(self)
        self.volumes = Volumes(self)
        self.usage = Usage(self)
        self.capacity = Capacity(self)

    def pricing(self) -> dict:
        return self._get("/v1/pricing")

    def whoami(self) -> dict:
        return self._get("/v1/whoami")

    def healthz(self) -> dict:
        return self._get("/v1/healthz")

    # -- transport ----------------------------------------------------------

    def _raise_for(self, r: httpx.Response) -> None:
        if r.status_code < 400:
            return
        try:
            detail = r.json().get("detail", {})
        except Exception:
            detail = {}
        raise NuminousError(detail.get("cause", "unknown"),
                            detail.get("message", r.text[:300]), r.status_code)

    def _get(self, path: str, params: dict | None = None) -> Any:
        r = self._http.get(path, params=params)
        self._raise_for(r)
        return r.json()

    def _post(self, path: str, body: dict, timeout: float = 120) -> Any:
        r = self._http.post(path, json=body, timeout=timeout)
        self._raise_for(r)
        return r.json()

    def _put(self, path: str, body: dict, timeout: float = 600) -> Any:
        r = self._http.put(path, json=body, timeout=timeout)
        self._raise_for(r)
        return r.json()

    def _delete(self, path: str) -> Any:
        r = self._http.delete(path)
        self._raise_for(r)
        return r.json()
