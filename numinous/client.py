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

    def __init__(self, cause: str, message: str, status: int,
                 retryable: bool | None = None):
        super().__init__(f"[{cause}] {message}")
        self.cause = cause
        self.message = message
        self.status = status
        self._retryable = retryable  # server hint, when present

    @property
    def is_provider_fault(self) -> bool:
        return self.cause.startswith("provider_")

    @property
    def retryable(self) -> bool:
        # Prefer the server's own hint: an org_quota from a concurrency/rate cap
        # frees on its own and is retryable; a hard budget cap is not. Fall back
        # to cause for older servers (capacity/infra clear; user_* never do).
        if self._retryable is not None:
            return self._retryable
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
               disk_gib: float | None = None, run_mode: str | None = None,
               gpu_mode: str | None = None,
               ttl_seconds: int = 0, launch_token: str | None = None,
               labels: dict[str, str] | None = None,
               egress: str = "allow", allow: list[str] | None = None,
               env: dict[str, str] | None = None,
               auto_suspend_idle_seconds: int = 0,
               volumes: list[dict] | None = None,
               gpu: int = 0, gpu_type: str | None = None,
               gpu_max_hr: float | None = None,
               plane: str = "auto",
               wait_for_slot: int | None = None) -> dict:
        """Create a sandbox.

        wait_for_slot: admission queue. Hold the create for up to N seconds
        while the org is over a self-freeing cap (concurrency, vCPU, memory,
        GPUs, start rate) and admit it when a slot frees, instead of raising
        NuminousError(org_quota, retryable=True) at once. Bounded by the org's
        admission_wait_sec setting (default 0 = queueing off, so this is a
        no-op until the org enables it). Budget caps never wait.
        """
        # gpu > 0 routes to the GPU plane on the server. Whether that plane is
        # a dedicated pod or a shared, multiplexed card is the platform's
        # decision; the client only states intent. No GPU-side code ships here.
        body = {
            "template_id": template_id, "image": image, "rom_id": rom_id,
            "vcpu": vcpu, "mem_gib": mem_gib, "disk_gib": disk_gib,
            "run_mode": run_mode, "gpu_mode": gpu_mode,
            "ttl_seconds": ttl_seconds,
            "launch_token": launch_token, "labels": labels or {},
            "network": {"egress": egress, "allow": allow or []},
            "env": env or {},
            "auto_suspend_idle_seconds": auto_suspend_idle_seconds,
            "volumes": volumes or [],
            "gpu": gpu, "plane": plane,
        }
        # Only send GPU fields when actually set. Sending an explicit null makes
        # a typed server reject the whole request (422), which is how SDK 0.1.7
        # broke every create; "absent" is what "use the default" must look like.
        if gpu_type is not None:
            body["gpu_type"] = gpu_type
        if gpu_max_hr is not None:
            body["gpu_max_hr"] = gpu_max_hr
        timeout = 600
        if wait_for_slot is not None:
            body["wait_for_slot_sec"] = int(wait_for_slot)
            timeout += int(wait_for_slot)
        return self._c._post("/v1/sandboxes", body, timeout=timeout)

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

    def fork(self, sandbox_id: str, count: int = 1, *,
             ttl_seconds: int | None = None,
             labels: dict | None = None) -> dict:
        """Fork a live sandbox into `count` children that start from its exact
        state (copy-on-write memory on the firecracker plane, committed rootfs
        on the docker plane). The parent keeps running. Returns
        {parent, snapshot_ref, requested, created, children:[...]}."""
        return self._c._post(f"/v1/sandboxes/{sandbox_id}/fork",
                             {"count": count, "ttl_seconds": ttl_seconds,
                              "labels": labels or {}}, timeout=900)

    def snapshots(self, sandbox_id: str, limit: int = 500) -> list[dict]:
        """The per-exec / per-tool-call snapshot timeline. Entries whose
        `command` starts with `tool:` were taken by the host tailer at agent
        tool-call boundaries; `kind=pre_exec` rows are state entering each
        exec. Each entry is a replay/fork point."""
        out = self._c._get(f"/v1/sandboxes/{sandbox_id}/snapshots",
                           params={"limit": limit})
        return out if isinstance(out, list) else out.get("items", [])

    def tree(self, sandbox_id: str) -> dict:
        """Fork lineage: ancestors and descendants of this sandbox."""
        return self._c._get(f"/v1/sandboxes/{sandbox_id}/tree")

    def set_egress(self, sandbox_id: str, egress: str,
                   allow: list[str] | None = None) -> dict:
        """Switch network policy on a RUNNING sandbox: allow | allowlist |
        deny. Enforced server-side without a reboot."""
        return self._c._post(f"/v1/sandboxes/{sandbox_id}/network",
                             {"egress": egress, "allow": allow or []})

    def exec_async_start(self, sandbox_id: str, command: str,
                         timeout_sec: float = 3600, *,
                         cwd: str | None = None,
                         env: dict[str, str] | None = None,
                         user: str | None = None) -> str:
        """Start a detached exec and return its exec_id. The command runs in
        the guest with streams on the guest's disk; it survives control-plane
        deploys and client disconnects. Poll with exec_async_poll, or block
        with exec_async_wait."""
        out = self._c._post(f"/v1/sandboxes/{sandbox_id}/exec/async",
                            {"command": command, "timeout_sec": timeout_sec,
                             "cwd": cwd, "env": env or {}, "user": user},
                            timeout=120)
        return out["exec_id"]

    def exec_async_poll(self, exec_id: str) -> dict:
        """Non-blocking status: {status: running|done|error, exit_code,
        stdout, stderr}."""
        return self._c._get(f"/v1/execs/{exec_id}")

    def exec_async_wait(self, exec_id: str, *, timeout: float = 3600,
                        poll: float = 2.0) -> dict:
        """Block until the detached exec reaches a terminal state. Transient
        poll errors are retried: that is the point of the async path."""
        deadline = time.monotonic() + timeout
        errors = 0
        while time.monotonic() < deadline:
            try:
                out = self.exec_async_poll(exec_id)
                errors = 0
            except Exception:
                errors += 1
                if errors >= 30:
                    raise
                time.sleep(poll)
                continue
            if out.get("status") in ("done", "error"):
                return out
            time.sleep(poll)
        raise TimeoutError(f"exec {exec_id} still running after {timeout}s")

    def execs(self, sandbox_id: str, limit: int = 500) -> dict:
        """Unified exec timeline (sync + async + tool-call snapshots)."""
        return self._c._get(f"/v1/sandboxes/{sandbox_id}/execs",
                            params={"limit": limit})

    def snapshot_stats(self, sandbox_id: str) -> dict:
        """Replay header aggregates: counts, bytes, per-tool breakdown."""
        return self._c._get(f"/v1/sandboxes/{sandbox_id}/snapshots/stats")

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


class Stats(_Resource):
    def rollup(self, label: str) -> dict:
        """Whole-trial rollup by label, e.g. 'harbor.session_id:trial-x'."""
        return self._c._get("/v1/rollup", params={"label": label})

    def concurrency(self, hours: int = 24, step_minutes: int = 5) -> dict:
        """Concurrent running sandboxes over time (the scale graph)."""
        return self._c._get("/v1/stats/concurrency",
                            params={"hours": hours,
                                    "step_minutes": step_minutes})


class Batches(_Resource):
    def submit(self, trials: list[dict], *, name: str = "",
               labels: dict | None = None) -> dict:
        """Submit trials as one batch. Each trial: {name, spec, depends_on}.
        spec is a sandbox-create body; depends_on lists trial names that must
        reach a terminal state first. Returns {id, state, trials}."""
        return self._c._post("/v1/batches",
                             {"name": name, "labels": labels or {},
                              "trials": trials}, timeout=120)

    def get(self, batch_id: str) -> dict:
        return self._c._get(f"/v1/batches/{batch_id}")


class Volumes(_Resource):
    def create(self, name: str, size_gib: float = 10.0,
               kind: str = "sandbox", datacenter: str | None = None) -> dict:
        """Idempotent by name within your org; safe to call on every start.

        kind="sandbox": host-local NVMe, mounts on CPU sandboxes ($0.10/GiB-mo).
        kind="gpu": RunPod network volume, datacenter-scoped, mounts on GPU
        sandboxes only ($0.10/GiB-mo). The two kinds live on different
        hardware and never cross.
        """
        body: dict = {"name": name, "size_gib": size_gib, "kind": kind}
        if datacenter:
            body["datacenter"] = datacenter
        return self._c._post("/v1/volumes", body)

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
        self.batches = Batches(self)
        self.stats = Stats(self)

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
        if isinstance(detail, list):
            # FastAPI validation errors ship detail as a list
            msg = "; ".join(
                f"{'.'.join(str(p) for p in e.get('loc', []))}: {e.get('msg', '')}"
                for e in detail if isinstance(e, dict)) or r.text[:300]
            raise NuminousError("validation", msg, r.status_code)
        if not isinstance(detail, dict):
            detail = {"message": str(detail)[:300]}
        raise NuminousError(detail.get("cause", "unknown"),
                            detail.get("message", r.text[:300]), r.status_code,
                            retryable=detail.get("retryable"))

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
