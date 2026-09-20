from __future__ import annotations

import os
import hashlib
from pathlib import Path
import re
import time
from typing import Any, Optional

import httpx

from ._build_context import pack_context


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


class Attribution:
    """Who ran what, and why: the platform's vocabulary for grouping work.
    Every field becomes a `numinous.<field>` label at create. Any harness can
    fill it (a run is an experiment / campaign / sweep; a trial is one unit of
    work whose retries share the id; source names the system that created it).
    Verdicts are added later with sandboxes.set_outcome()."""

    def __init__(self, *, run: str | None = None, run_name: str | None = None, trial: str | None = None,
                 task: str | None = None, agent: str | None = None, model: str | None = None,
                 source: str | None = None, source_ref: str | None = None, session: str | None = None,
                 attempt: int | None = None, extra: dict[str, str] | None = None):
        self.fields = {k: v for k, v in dict(run=run, run_name=run_name, trial=trial, attempt=attempt, task=task, agent=agent, model=model,
                                              source=source, source_ref=source_ref, session=session).items() if v}
        self.extra = dict(extra or {})

    def labels(self) -> dict[str, str]:
        return {**self.extra, **{f"numinous.{k}": str(v) for k, v in self.fields.items()}}


class _Resource:
    def __init__(self, c: "Numinous"):
        self._c = c


class Templates(_Resource):
    def pack(self, name: str, *, image: str | None = None,
             dockerfile: str | None = None, context: str | None = None,
             warm_cmd: str | None = None) -> dict:
        """Build from an image or inline Dockerfile and optional local context.

        An omitted context is empty. An explicit context is uploaded from the
        client, with bounded sizes and .dockerignore filtering. Symlinks,
        special files and unsupported ignore syntax fail before HTTP.
        """
        if bool(image) == bool(dockerfile) or (context is not None and (image or not context)):
            raise ValueError("Choose exactly one image or Dockerfile; context requires a Dockerfile")
        source: dict[str, Any] = {}
        if image:
            source = {"type": "image", "image": image, "warm_cmd": warm_cmd}
        elif dockerfile:
            source = {"type": "dockerfile", "dockerfile": dockerfile,
                      "warm_cmd": warm_cmd}
            if context is not None:
                source["context_tar_b64"] = pack_context(context)
        return self._c._post("/v1/templates", {"name": name, "source": source},
                             timeout=1800)

    def list(self, *, include_retired: bool = False) -> list[dict]:
        """Ready and building templates. Retired ones are tombstones and are
        left out unless asked for."""
        return self._c._get("/v1/templates" + ("?include_retired=true" if include_retired else ""))

    def retire(self, template_id: str) -> dict:
        """Take a template out of service: nothing new boots from it, and
        sandboxes already running from it are untouched. Refused with 409
        while it still has live sandboxes."""
        return self._c._delete(f"/v1/templates/{template_id}")

    def manifest(self, template_id: str) -> dict:
        """What the template contains, recorded at build (OS release, package
        inventories, file tree digest, binaries)."""
        return self._c._get(f"/v1/templates/{template_id}/manifest")

    def manifest_diff(self, template_id: str, against: str) -> dict:
        """Drift between two builds: packages added, removed, changed; tree digest; binaries."""
        return self._c._get(f"/v1/templates/{template_id}/manifest/diff", params={"against": against})

    def get(self, template_id: str) -> dict:
        return self._c._get(f"/v1/templates/{template_id}")


class Snapshots(_Resource):
    def pin(self, snapshot_id: str) -> dict:
        """Exempt one checkpoint from both retention clocks until unpinned
        (its sandbox's memory chain is held with it). Standard storage rate."""
        return self._c._post(f"/v1/snapshots/{snapshot_id}/pin", {})

    def unpin(self, snapshot_id: str) -> dict:
        return self._c._delete(f"/v1/snapshots/{snapshot_id}/pin")

    def file(self, snapshot_id: str, path: str, *, max_bytes: int = 16 << 20) -> dict:
        """A file exactly as it was at the checkpoint, read from the
        checkpoint's root disk without booting it. `content_b64` holds the
        bytes; `kind` is regular, directory or symlink."""
        return self._c._get(f"/v1/snapshots/{snapshot_id}/files", params={"path": path, "max_bytes": max_bytes})

    def read(self, snapshot_id: str, path: str, *, max_bytes: int = 16 << 20) -> bytes:
        import base64
        out = self.file(snapshot_id, path, max_bytes=max_bytes)
        return base64.b64decode(out["content_b64"]) if out.get("content_b64") else b""

    def listdir(self, snapshot_id: str, path: str = "/") -> list[dict]:
        return self._c._get(f"/v1/snapshots/{snapshot_id}/files", params={"path": path, "list": 1})["entries"]

    def fork(self, snapshot_id: str, count: int = 1, *, ttl_seconds: int | None = None,
             labels: dict | None = None) -> dict:
        """Start `count` sandboxes from this checkpoint's exact state.

        sandboxes.fork() branches a machine that is still running; this
        branches a recorded moment, so a trial can be reopened at any step
        long after it ended. Returns {snapshot, requested, created,
        children:[...]}."""
        body: dict = {"count": count, "labels": labels or {}}
        if ttl_seconds is not None:
            body["ttl_seconds"] = ttl_seconds
        return self._c._post(f"/v1/snapshots/{snapshot_id}/fork", body, timeout=900)


class Roms(_Resource):
    def create(self, name: str, files: dict[str, str]) -> dict:
        return self._c._post("/v1/roms", {"name": name, "files": files})


class Sandboxes(_Resource):
    def create(self, *, template_id: str | None = None, image: str | None = None,
               rom_id: str | None = None, vcpu: int = 2, mem_gib: float = 4.0,
               disk_gib: float | None = None, run_mode: str | None = None,
               gpu_mode: str | None = None, gpu_oversubscribe: bool = False,
               ttl_seconds: int = 0, launch_token: str | None = None,
               labels: dict[str, str] | None = None,
               egress: str = "allow", allow: list[str] | None = None,
               env: dict[str, str] | None = None,
               auto_suspend_idle_seconds: int = 0,
               inference_sleep: bool = False,
               volumes: list[dict] | None = None,
               snapshot_policy: str | None = None,
               snapshot_retention: int | None = None,
               snapshot_ttl_seconds: int | None = None,
               memory_ttl_seconds: int | None = None,
               retention: str | dict | None = None,
               model_spend_pause_usd: float | None = None,
               gpu_fraction: float | None = None,
               gpu: int = 0, gpu_type: str | None = None,
               gpu_max_hr: float | None = None,
               plane: str = "auto",
               wait: bool = True,
               wait_for_slot: int | None = None,
               retry_admission_sec: float = 0.0,
               attribution: Attribution | None = None) -> dict:
        """Create a sandbox.

        wait: True (default) returns when the sandbox is running. False returns
        as soon as the create is admitted and recorded (state "creating"); the
        sandbox boots in the background and reaches "running" or a typed
        failure moments later. Use wait=False for fan-out and follow with
        wait_running(), or use create_many() for one request.

        snapshot_ttl_seconds: optional age limit for replay points (for example
        7 days = 604800). Independent of snapshot_retention (a count); either
        retires a point. Change later with snapshot_policy().

        attribution: Attribution(run=, trial=, task=, agent=, ...) becomes
        `numinous.*` labels so the console groups this sandbox into its trial
        and run and links back to its source. Merged over `labels`.

        inference_sleep: opt in to the firecracker inference-sleep monitor.
        Runtime options belong in request fields, not reserved labels.

        wait_for_slot: SERVER-side admission queue. Hold the create for up to
        N seconds while the org is over a self-freeing cap (concurrency, vCPU,
        memory, GPUs, start rate) and admit it when a slot frees, instead of
        raising NuminousError(org_quota, retryable=True) at once. Bounded by
        the org's admission_wait_sec setting (default 0 = queueing off, so
        this is a no-op until the org enables it). Budget caps never wait.

        retry_admission_sec: CLIENT-side backoff. Retry retryable admission
        refusals (429 org_quota retryable, 503 provider_capacity) with
        exponential backoff for up to this many seconds. 0 = raise at once.
        """
        # gpu > 0 routes to the GPU plane on the server. Whether that plane is
        # a dedicated pod or a shared, multiplexed card is the platform's
        # decision; the client only states intent. No GPU-side code ships here.
        body = {
            "template_id": template_id, "image": image, "rom_id": rom_id,
            "vcpu": vcpu, "mem_gib": mem_gib, "disk_gib": disk_gib,
            "run_mode": run_mode, "gpu_mode": gpu_mode, "gpu_oversubscribe": gpu_oversubscribe,
            "ttl_seconds": ttl_seconds,
            "launch_token": launch_token, "labels": {**(labels or {}), **(attribution.labels() if attribution else {})},
            "network": {"egress": egress, "allow": allow or []},
            "env": env or {},
            "auto_suspend_idle_seconds": auto_suspend_idle_seconds,
            "inference_sleep": inference_sleep,
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
        # Checkpointing: "auto" (default: continuous on microVMs; every
        # machine-observed step boundary and every exec), "per_exec",
        # "manual" or "off"; snapshot_retention keeps the newest N replay
        # points live.
        if snapshot_policy is not None:
            body["snapshot_policy"] = snapshot_policy
        if snapshot_retention is not None:
            body["snapshot_retention"] = int(snapshot_retention)
        if snapshot_ttl_seconds is not None:
            body["snapshot_ttl_seconds"] = int(snapshot_ttl_seconds)
        if memory_ttl_seconds is not None:
            body["memory_ttl_seconds"] = int(memory_ttl_seconds)
        if retention is not None:
            # a named strategy ("default", "memory_7d", "7d", "30d", "forever")
            # or {"memory_ttl_seconds": ..., "snapshot_ttl_seconds": ...}; 0 = never
            if isinstance(retention, dict):
                for k in ("memory_ttl_seconds", "snapshot_ttl_seconds"):
                    if retention.get(k) is not None:
                        body[k] = int(retention[k])
            else:
                body["retention"] = str(retention)
        if model_spend_pause_usd is not None:
            body["model_spend_pause_usd"] = float(model_spend_pause_usd)
        if gpu_fraction is not None:
            # a persistent share of one pooled card (gpu_mode="shared"):
            # SM cap and memory cap, billed by occupancy at that fraction
            body["gpu_fraction"] = float(gpu_fraction)
        # A GPU pod can take minutes to pull its image; holding one HTTP
        # request open for that lost the sandbox id when the client gave up
        # (a pod ran unowned for 13 minutes, 2026-09-20). GPU creates are
        # acknowledged at once and polled here until running or terminal.
        poll_after = wait and gpu > 0
        if not wait or poll_after:
            body["wait"] = False
        timeout = 600
        if wait_for_slot is not None:
            body["wait_for_slot_sec"] = int(wait_for_slot)
            timeout += int(wait_for_slot)
        deadline = time.monotonic() + max(0.0, retry_admission_sec)
        delay = 2.0
        while True:
            try:
                out = self._c._post("/v1/sandboxes", body, timeout=timeout)
                if poll_after and out.get("state") == "creating":
                    return self.wait(out["id"], until="running", timeout=1800, poll=5.0)
                return out
            except NuminousError as e:
                admission = (e.status == 429 and e.cause == "org_quota") or (
                    e.status == 503 and e.cause == "provider_capacity")
                left = deadline - time.monotonic()
                if not (admission and e.retryable) or left <= 0:
                    raise
                time.sleep(min(delay, left))
                delay = min(delay * 1.7, 30.0)

    def get(self, sandbox_id: str) -> dict:
        return self._c._get(f"/v1/sandboxes/{sandbox_id}")

    def create_many(self, specs: list[dict], *, retry_admission_sec: float = 0.0) -> dict:
        """Admit many sandboxes in one request (up to 500).

        Each spec takes the same keyword arguments as create(). Every item is
        admitted independently and boots in the background; the answer lists
        each item as {"ok": True, ...sandbox} or {"ok": False, "status", "cause",
        "message"} in place. Follow with wait_running() on the admitted ids."""
        items = []
        for spec in specs:
            spec = dict(spec)
            attribution = spec.pop("attribution", None)
            labels = {**(spec.pop("labels", None) or {}), **(attribution.labels() if attribution else {})}
            body = {"template_id": spec.pop("template_id", None), "image": spec.pop("image", None),
                    "rom_id": spec.pop("rom_id", None), "vcpu": spec.pop("vcpu", 2), "mem_gib": spec.pop("mem_gib", 4.0),
                    "ttl_seconds": spec.pop("ttl_seconds", 0), "labels": labels,
                    "network": {"egress": spec.pop("egress", "allow"), "allow": spec.pop("allow", None) or []},
                    "env": spec.pop("env", None) or {}, "plane": spec.pop("plane", "auto"),
                    "gpu": spec.pop("gpu", 0), "volumes": spec.pop("volumes", None) or []}
            for key in ("disk_gib", "run_mode", "gpu_mode", "gpu_type", "gpu_max_hr", "snapshot_policy",
                        "snapshot_retention", "snapshot_ttl_seconds", "auto_suspend_idle_seconds",
                        "inference_sleep", "launch_token"):
                if spec.get(key) is not None:
                    body[key] = spec[key]
            items.append(body)
        deadline = time.monotonic() + max(0.0, retry_admission_sec)
        delay = 2.0
        while True:
            try:
                return self._c._post("/v1/sandboxes/batch", {"items": items}, timeout=900)
            except NuminousError as e:
                if not (e.retryable and time.monotonic() < deadline):
                    raise
                time.sleep(delay); delay = min(delay * 2, 20.0)

    def wait_running(self, ids: list[str] | str, *, timeout: float = 600.0, poll: float = 1.0) -> dict[str, dict]:
        """Wait until each sandbox has left `creating`. Returns id -> sandbox.
        A sandbox that failed to boot is returned with its typed cause; it is
        not raised, so a fan-out reports per item."""
        pending = set([ids] if isinstance(ids, str) else ids)
        out: dict[str, dict] = {}
        deadline = time.monotonic() + timeout
        while pending and time.monotonic() < deadline:
            for sid in list(pending):
                sb = self.get(sid)
                if sb.get("state") != "creating":
                    out[sid] = sb
                    pending.discard(sid)
            if pending:
                time.sleep(poll)
        for sid in pending:
            out[sid] = {"id": sid, "state": "creating", "cause": "wait_timeout"}
        return out

    def snapshot_policy(self, sandbox_id: str, *, retention: int | None = None,
                        ttl_seconds: int | None = None, memory_ttl_seconds: int | None = None,
                        preset: str | None = None, inherit: bool = False) -> dict:
        """Change how long checkpoints are kept, at any time.

        Two clocks start when the sandbox ends: `memory_ttl_seconds` drops the
        memory (bases and diffs; every checkpoint stays as a filesystem
        checkpoint) and `ttl_seconds` retires the checkpoint entirely. 0 means
        never. `preset` names a strategy: "default" (memory 24 h, filesystem
        7 d), "memory_7d" (7 d / 30 d), "7d", "30d", "forever". `inherit=True`
        clears both clocks back to the org's default. `retention` is the
        optional count cap. Pinned checkpoints are exempt from both clocks.
        Returns the effective policy."""
        body: dict = {}
        if retention is not None:
            body["snapshot_retention"] = int(retention)
        if ttl_seconds is not None:
            body["snapshot_ttl_seconds"] = int(ttl_seconds)
        if memory_ttl_seconds is not None:
            body["memory_ttl_seconds"] = int(memory_ttl_seconds)
        if preset is not None:
            body["retention"] = preset
        if inherit:
            body["inherit"] = True
        return self._c._patch(f"/v1/sandboxes/{sandbox_id}/snapshot-policy", body)

    def set_outcome(self, sandbox_id: str, value: float | None, *, kind: str = "reward",
                    label: str | None = None, status: str | None = None) -> dict:
        """Record the harness's verdict on this attempt: numinous.outcome (a
        number) and/or outcome_label (a category), outcome_kind, status. The
        trial shows the latest attempt's verdict until trials.settle() is
        called; the platform does no analytics on it."""
        labels: dict[str, str | None] = {"numinous.outcome": None if value is None else str(value),
                                         "numinous.outcome_kind": kind}
        if label is not None:
            labels["numinous.outcome_label"] = label
        if status is not None:
            labels["numinous.status"] = status
        return self.set_labels(sandbox_id, labels)

    def fanout(self, count: int, *, template_id: str | None = None, image: str | None = None,
               size: str | None = None, vcpu: int = 2, mem_gib: float = 4.0, ttl_seconds: int | None = None,
               labels: dict[str, str] | None = None, env: dict[str, str] | None = None,
               network: dict | None = None, snapshot_policy: str | None = None,
               disk_gib: float | None = None) -> dict:
        """Create `count` identical sandboxes (1..100) with ONE credit
        reservation and no wait on any worker. Every item comes back in
        state `creating`; readiness is observed by get() or by exec().

        Returns {lease_id, admitted, lease_admission, items}. Each item is
        either {ok: true, ...sandbox} or a typed per-item refusal
        {ok: false, status, cause, retryable, message}, so a partial fit is
        reported in place rather than failing the whole request.

        Volumes, ROMs and GPUs are not on this path (typed 422); use
        create() for those. This is the burst path for CPU trials.
        """
        body: dict[str, Any] = {"count": count, "vcpu": vcpu, "mem_gib": mem_gib}
        if template_id: body["template_id"] = template_id
        if image: body["image"] = image
        if size: body["size"] = size
        if ttl_seconds: body["ttl_seconds"] = ttl_seconds
        if labels: body["labels"] = labels
        if env: body["env"] = env
        if network: body["network"] = network
        if snapshot_policy: body["snapshot_policy"] = snapshot_policy
        if disk_gib: body["disk_gib"] = disk_gib
        return self._c._post("/v1/sandboxes/fanout", body, timeout=300)

    def set_labels(self, sandbox_id: str, labels: dict[str, str | None]) -> dict:
        """Merge labels onto a sandbox in any state (null deletes a key).
        Harnesses learn the experiment, agent, and reward after create; the
        console groups and grades by these labels."""
        return self._c._patch(f"/v1/sandboxes/{sandbox_id}/labels", {"labels": labels})

    def list(self, *, label: str | list[str] | None = None, state: str | None = None,
             limit: int | None = None, offset: int = 0) -> list[dict]:
        """Sandboxes visible to this key (the org's). Unpaged by default for
        compatibility; pass limit (<= 200) to page, or use list_all()."""
        params: dict[str, Any] = {}
        if label:
            params["label"] = label
        if state:
            params["state"] = state
        if limit is not None:
            params["limit"] = limit
            params["offset"] = offset
        out = self._c._get("/v1/sandboxes", params=params)
        return out if isinstance(out, list) else out.get("items", [])

    def list_page(self, *, label: str | list[str] | None = None, state: str | None = None,
                  limit: int = 200, offset: int = 0) -> dict:
        """One page: {items, total, limit, offset}."""
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if label:
            params["label"] = label
        if state:
            params["state"] = state
        return self._c._get("/v1/sandboxes", params=params)

    def list_all(self, *, label: str | list[str] | None = None, state: str | None = None,
                 page: int = 200, max_pages: int = 100) -> list[dict]:
        """Every matching sandbox, walking pages of `page` until `total`."""
        out: list[dict] = []
        off = 0
        for _ in range(max_pages):
            pg = self.list_page(label=label, state=state, limit=page, offset=off)
            out += pg.get("items", [])
            off += page
            if off >= int(pg.get("total", 0)):
                break
        return out

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

    def export(self, sandbox_id: str, path: str, to: str | None = None,
               *, request_token: str | None = None) -> dict:
        """Publish an archive; use exports.download with the returned export_id.

        Reuse request_token after a lost response to avoid another export.
        Post-termination creation requires a retained filesystem on the driver.
        Already-published exports are independent of sandbox lifetime.
        """
        return self._c._post(f"/v1/sandboxes/{sandbox_id}/export",
                             {"path": path, "to": to, "request_token": request_token}, timeout=1800)

    def report_spend(self, sandbox_id: str, *, source: str, kind: str = "usage",
                     amount_usd: float = 0.0, tokens_in: int = 0, tokens_out: int = 0,
                     cumulative_usd: float | None = None, note: dict | None = None) -> dict:
        """Report model spend against a sandbox, or that its model credit ran
        out (`credit_exhausted`) or came back (`credit_restored`).

        Reaching the `model_spend_pause_usd` set at create, or an exhaustion
        signal, pauses the sandbox: suspended in ~34 ms, billed for storage
        only, TTL clock stopped. A restoration signal or resume() continues it
        exactly where it was. Pausing is a state, never a failure."""
        return self._c._post(f"/v1/sandboxes/{sandbox_id}/spend",
                             {"source": source, "kind": kind, "amount_usd": amount_usd,
                              "tokens_in": tokens_in, "tokens_out": tokens_out,
                              "cumulative_usd": cumulative_usd, "note": note or {}})

    def spend(self, sandbox_id: str) -> dict:
        """Model spend reported so far, the budget, and whether the sandbox is
        paused for it."""
        return self._c._get(f"/v1/sandboxes/{sandbox_id}/spend")

    def network_manifest(self, sandbox_id: str) -> dict:
        """What egress policy actually applied to this sandbox, and which
        mechanism enforced it. This is the record to hand an auditor: the
        resolver pinned every address it returned, the kernel permitted only
        those, and the splicer checked the server name."""
        return self._c._get(f"/v1/sandboxes/{sandbox_id}/network/manifest")

    def activity(self, sandbox_ids: list[str]) -> dict:
        """Last observed activity for many sandboxes in one call, for a
        supervisor deciding what is stalled without polling each one."""
        return self._c._post("/v1/sandboxes/activity", {"ids": list(sandbox_ids)})

    def destroy(self, sandbox_id: str) -> dict:
        """Returns the sandbox with teardown_proof attached."""
        return self._c._delete(f"/v1/sandboxes/{sandbox_id}")


    # One inline request is bounded by the control plane's inline_file_max_mib
    # (47 MiB by default), and a larger file must arrive in pieces that the
    # guest appends. A caller should not have to know that: put_file splits.
    PIECE_BYTES = 32 << 20

    def put_file(self, sandbox_id: str, path: str, data: bytes,
                 *, piece_bytes: int | None = None) -> dict:
        """Write bytes to a path inside the sandbox.

        Anything larger than one request is sent as appended pieces, so a
        multi-gigabyte file is a normal call. The last piece commits: until it
        lands the destination is untouched, so a failure mid-transfer never
        leaves a half-written file where the workload can read it.

        Mode bits are not part of the write; run `chmod` with exec() when a
        script must be executable.
        """
        import base64
        piece = int(piece_bytes or self.PIECE_BYTES)
        if piece < 1:
            raise ValueError("piece_bytes must be positive")
        body = {"path": path}
        if len(data) <= piece:
            return self._c._put(f"/v1/sandboxes/{sandbox_id}/files",
                                {**body, "content_b64": base64.b64encode(data).decode()})
        result = None
        for offset in range(0, len(data), piece):
            chunk = data[offset:offset + piece]
            last = offset + piece >= len(data)
            result = self._c._put(f"/v1/sandboxes/{sandbox_id}/files",
                                  {**body, "content_b64": base64.b64encode(chunk).decode(),
                                   "append": offset > 0, "final": last})
        return result

    def get_file(self, sandbox_id: str, path: str) -> bytes:
        import base64
        out = self._c._get(f"/v1/sandboxes/{sandbox_id}/files",
                           params={"path": path})
        return base64.b64decode(out["content_b64"])

    def checkpoint(self, sandbox_id: str, name: str | None = None) -> dict:
        """Freeze a running sandbox into a new template (fork point)."""
        return self._c._post(f"/v1/sandboxes/{sandbox_id}/checkpoint",
                             {"name": name}, timeout=900)

    def snapshot(self, sandbox_id: str, label: str | None = None) -> dict:
        """Take a replay point now (memory + filesystem). Returns the
        snapshot row (id, size_bytes, sha256); it appears in snapshots()
        and can be forked with fork(snapshot_id=...)."""
        return self._c._post(f"/v1/sandboxes/{sandbox_id}/snapshot",
                             {"label": label} if label else {}, timeout=300)

    def fork(self, sandbox_id: str, count: int = 1, *,
             ttl_seconds: int | None = None,
             labels: dict | None = None,
             wait: bool = True, timeout: float = 300) -> dict:
        """Fork a live sandbox into `count` children that start from its exact
        state (copy-on-write memory on the firecracker plane, committed rootfs
        on the docker plane). The parent keeps running. Returns
        {parent, snapshot_ref, requested, created, children:[...]}.

        The control plane acknowledges a fork before the children have
        booted (they come back `creating`). With wait=True (default) each
        child is polled to `running` or a terminal state and the returned
        children carry their final rows; wait=False returns the ack as is."""
        body: dict = {"count": count, "labels": labels or {}}
        if ttl_seconds is not None:   # the API takes an integer or nothing; null is refused
            body["ttl_seconds"] = ttl_seconds
        out = self._c._post(f"/v1/sandboxes/{sandbox_id}/fork", body, timeout=900)
        if wait:
            out["children"] = self._settle_children(out.get("children") or [], timeout)
        return out

    def _settle_children(self, children: list[dict], timeout: float) -> list[dict]:
        deadline = time.monotonic() + timeout
        settled: list[dict] = []
        for ch in children:
            if ch.get("state") in ("running", "failed", "terminated"):
                settled.append(ch); continue
            sid = ch["id"]
            while True:
                row = self.get(sid)
                if row["state"] in ("running", "failed", "terminated") or time.monotonic() >= deadline:
                    settled.append(row); break
                time.sleep(0.5)
        return settled

    def snapshots(self, sandbox_id: str, limit: int = 500, *,
                  verify: bool = False) -> list[dict]:
        """The per-exec / per-tool-call snapshot timeline. Entries whose
        `command` starts with `tool:` were taken by the host tailer at agent
        tool-call boundaries; `kind=pre_exec` rows are state entering each
        exec. Each entry is a replay/fork point.

        verify=True asks the control plane to HEAD each durable_ref in object
        storage and adds `durable_verified` (True/False/None) per entry."""
        params: dict[str, Any] = {"limit": limit}
        if verify:
            params["verify"] = 1
        out = self._c._get(f"/v1/sandboxes/{sandbox_id}/snapshots", params=params)
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

    def metrics(self, sandbox_id: str, *, limit: int = 2000,
                before: int | None = None, until: str | None = None) -> dict:
        """Bounded resource page, oldest first. Follow window.next_before for older pages.

        until is an ISO-8601 timestamp with timezone. Averages describe this page;
        recording.retained_samples counts all retained observations.
        """
        params = {"limit": limit}
        if before is not None:
            params["before"] = before
        if until is not None:
            params["until"] = until
        return self._c._get(f"/v1/sandboxes/{sandbox_id}/metrics", params=params)

    def events(self, sandbox_id: str) -> list[dict]:
        return self._c._get(f"/v1/sandboxes/{sandbox_id}/events")

    # ---- machine-observed steps -------------------------------------------

    def steps(self, sandbox_id: str, *, full: bool = False, limit: int = 2000) -> dict:
        """Machine-observed steps, oldest first. A step is a boundary the guest
        kernel reported (a finished process subtree or a burst of file
        changes), the checkpoint taken there and the machine measured at that
        moment. `full=True` adds the bounded record (lead process, accounting,
        tests, file changes, top processes, network flows)."""
        return self._c._get(f"/v1/sandboxes/{sandbox_id}/steps", params={"full": int(full), "limit": limit})

    def steps_series(self, sandbox_id: str, fields: list[str] | None = None) -> dict:
        """Columnar series aligned on `t` for charts; null means not measured."""
        params = {"fields": ",".join(fields)} if fields else None
        return self._c._get(f"/v1/sandboxes/{sandbox_id}/steps/series", params=params)

    def network(self, sandbox_id: str) -> dict:
        """What the workload reached and what stopped it, observed from
        inside: remotes with names, bytes and outcomes, DNS names, denials
        with the observed signature, policy changes and in-guest probes."""
        return self._c._get(f"/v1/sandboxes/{sandbox_id}/network")

    def steps_diff(self, sandbox_id: str, *, since: int = 0, until: int | None = None) -> dict:
        """Files changed between two checkpoints, folded from the per-step
        deltas: operations, sizes and source hashes at both ends."""
        params: dict = {"since": since}
        if until is not None:
            params["until"] = until
        return self._c._get(f"/v1/sandboxes/{sandbox_id}/steps/diff", params=params)

    def cost(self, sandbox_id: str) -> dict:
        """The compute charge decomposed into steps (busy versus idle CPU),
        boot and tail; segments sum to the charge exactly."""
        return self._c._get(f"/v1/sandboxes/{sandbox_id}/cost")

    def timeline(self, sandbox_id: str) -> dict:
        """Everything that happened to the sandbox in one ordered record."""
        return self._c._get(f"/v1/sandboxes/{sandbox_id}/timeline")

    def rerun_step(self, sandbox_id: str, seq: int, *, command: str | None = None, timeout_sec: int = 600,
                   labels: dict[str, str] | None = None) -> dict:
        """Rewind to step `seq` and run the next step again in a forked child;
        returns the child, the exec with accounting, and the step comparison."""
        return self._c._post(f"/v1/sandboxes/{sandbox_id}/steps/{seq}/rerun",
                             {"command": command, "timeout_sec": timeout_sec, "labels": labels or {}}, timeout=timeout_sec + 300)

    def compare_steps(self, sandbox_id: str, against: str, *, offset: int = 0, offset_against: int = 0) -> dict:
        """Two sandboxes step by step on the same machine ruler."""
        return self._c._get(f"/v1/sandboxes/{sandbox_id}/steps/compare",
                            params={"against": against, "offset": offset, "offset_against": offset_against})

    def exec_output(self, sandbox_id: str, exec_id: str, *, stream: str = "stdout") -> bytes:
        """The full stream a command wrote when the exec response truncated
        it (published by the worker after the command finished). Raises
        NuminousError(cause=output_not_published) while it is still being
        published and output_not_truncated when the response already held
        everything."""
        r = self._c._http.get(f"/v1/sandboxes/{sandbox_id}/execs/{exec_id}/output", params={"stream": stream})
        self._c._raise_for(r)
        return r.content

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
    def checkpoints(self, days: int = 7) -> dict:
        """Checkpoints-per-trial distribution for this org over `days`."""
        return self._c._get("/v1/stats/checkpoints", params={"days": days})

    def rollup(self, label: str) -> dict:
        """Whole-trial rollup by label, e.g. 'harbor.session_id:trial-x'."""
        return self._c._get("/v1/rollup", params={"label": label})

    def summary(self, days: int = 7, tz: str = "UTC") -> dict:
        """Org totals over `days`: sandboxes, compute spans, spend, faults."""
        return self._c._get("/v1/stats", params={"days": days, "tz": tz})

    def latency(self, days: int = 14, *, driver: str | None = None, template_id: str | None = None) -> dict:
        """p50/p90/p95/p99 for startup, fork, resume and checkpoint pause
        from the platform's records of this org's sandboxes over `days`.
        Each series carries its definition, its source rows, n, min, max."""
        params = {"days": days}
        if driver:
            params["driver"] = driver
        if template_id:
            params["template_id"] = template_id
        return self._c._get("/v1/latency", params=params)

    def timeseries(self, *, hours: str = "24", step: int = 5, tz: str = "UTC",
                   run: str | None = None, agent: str | None = None,
                   task: str | None = None,
                   filters: dict[str, str] | None = None) -> dict:
        """Bucketed activity over time, filtered the same way trials are.
        `hours` is a string because the API also accepts spans like '7d'."""
        params: dict[str, Any] = {"hours": hours, "step": step, "tz": tz}
        if run:
            params["experiment"] = run
        if agent:
            params["agent"] = agent
        if task:
            params["task"] = task
        if filters:
            params["filter"] = [f"{k}:{v}" for k, v in filters.items()]
        return self._c._get("/v1/stats/timeseries", params=params)

    def concurrency(self, hours: int = 24, step_minutes: int = 5) -> dict:
        """Concurrent running sandboxes over time (the scale graph)."""
        return self._c._get("/v1/stats/concurrency",
                            params={"hours": hours,
                                    "step_minutes": step_minutes})


class Billing(_Resource):
    def balance(self) -> dict:
        """This org's credit position: granted, consumed, remaining, and each
        grant with its expiry. Credits are prepaid and consumed FIFO, so the
        first entry is the one being drawn down now."""
        return self._c._get("/v1/billing")


class Labels(_Resource):
    def values(self, key: str, *, q: str = "", limit: int = 50,
               offset: int = 0) -> dict:
        """Distinct values seen for a label key, for building a filter UI
        without hard-coding what a harness happens to emit."""
        params: dict[str, Any] = {"key": key, "limit": limit, "offset": offset}
        if q:
            params["q"] = q
        return self._c._get("/v1/labels/values", params=params)


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
               kind: str = "sandbox", datacenter: str | None = None,
               plane: str | None = None) -> dict:
        """Idempotent by name within your org; safe to call on every start.

        kind="sandbox": a persistent volume for CPU sandboxes ($0.10/GiB-mo).
        On the Firecracker plane (the default when that pool exists) it is a
        durable ext4 image attached to the microVM as a block device: one
        sandbox at a time, flushed to durable storage before that sandbox's
        teardown completes, then attachable by the next sandbox on any
        worker. plane="docker" is a host-local Docker volume on one worker.
        kind="gpu": RunPod network volume, datacenter-scoped, mounts on GPU
        sandboxes only ($0.10/GiB-mo). The kinds live on different hardware
        and never cross.
        """
        body: dict = {"name": name, "size_gib": size_gib, "kind": kind}
        if datacenter:
            body["datacenter"] = datacenter
        if plane:
            body["plane"] = plane
        return self._c._post("/v1/volumes", body)

    def get(self, volume_id: str) -> dict:
        """One volume with its attachment state (attached_sandbox_id,
        attach_generation, flushed_at, last flush manifest)."""
        return self._c._get(f"/v1/volumes/{volume_id}")

    def list(self) -> list[dict]:
        return self._c._get("/v1/volumes")

    def delete(self, volume_id: str) -> dict:
        return self._c._delete(f"/v1/volumes/{volume_id}")


class Usage(_Resource):
    def query(self, *, label: str | None = None, since: str | None = None,
              sandbox_id: str | None = None) -> dict:
        """Billed spans for this org: {spans, total_cost_usd,
        unbilled_provider_fault_usd}. `since` is ISO-8601."""
        params: dict[str, Any] = {}
        if label:
            params["label"] = label
        if since:
            params["since"] = since
        if sandbox_id:
            params["sandbox_id"] = sandbox_id
        return self._c._get("/v1/usage", params=params)


class AttributionConfig(_Resource):
    def get(self, days: int = 7) -> dict:
        """Schema, this org's aliases and link templates, and attribution
        coverage over `days` (what share of sandboxes carry run/trial/outcome)."""
        return self._c._get("/v1/attribution", params={"days": days})

    def set(self, *, presets: list[str] | None = None, aliases: dict[str, str] | None = None, sources: list[dict] | None = None) -> dict:
        """presets: integrations to enable (see get()["presets"]["available"]).
        aliases: foreign label key -> numinous.<field>. sources: [{match, url, name}]
        where match is a regex on numinous.source_ref and url may use {run} {trial} {ref} {1}.."""
        body: dict[str, Any] = {}
        if presets is not None:
            body["presets"] = list(presets)
        if aliases is not None:
            body["aliases"] = aliases
        if sources is not None:
            body["sources"] = sources
        return self._c._put("/v1/attribution", body, timeout=60)

    def backfill(self, days: int = 3650) -> dict:
        """Re-run ingest over history: normalize labels (adds canonical keys,
        removes nothing) and rebuild this org's trials and runs."""
        return self._c._post(f"/v1/attribution/backfill?days={int(days)}", {}, timeout=600)


class Trials(_Resource):
    """Trials: one harness unit of work with every retry attempt, materialized
    by the platform from attribution labels."""

    def list(self, *, run: str | None = None, agent: str | None = None, task: str | None = None,
             filters: dict[str, str] | None = None, state: str | None = None, cause: str | None = None,
             q: str | None = None, days: int = 30, limit: int = 50, offset: int = 0, sort: str = "started") -> dict:
        params: dict[str, Any] = {"days": days, "limit": limit, "offset": offset, "sort": sort}
        if run: params["experiment"] = run
        if agent: params["agent"] = agent
        if task: params["task"] = task
        if state: params["state"] = state
        if cause: params["cause"] = cause
        if q: params["q"] = q
        if filters:
            params["filter"] = [f"{k}:{v}" for k, v in filters.items()]
        return self._c._get("/v1/trials", params=params)

    def list_all(self, **kw) -> list[dict]:
        out: list[dict] = []
        offset = 0
        while True:
            page = self.list(offset=offset, limit=200, **kw)
            out.extend(page["items"])
            offset += len(page["items"])
            if not page["items"] or offset >= page["total"]:
                return out

    def get(self, trial: str) -> dict:
        """The trial with `attempt_list`: every sandbox, in attempt order."""
        return self._c._get(f"/v1/trials/{trial}/attempts")

    def settle(self, trial: str, value: float | None = None, *, label: str | None = None,
               kind: str = "reward", status: str | None = None, force: bool = False) -> dict:
        """Settle the trial's final verdict. Attempt-level outcomes set with
        sandboxes.set_outcome() are provisional; this one is final."""
        return self._c._put(f"/v1/trials/{trial}/outcome", {"value": value, "label": label, "kind": kind, "status": status, "force": force})

    def steps(self, trial: str, *, full: bool = False) -> dict:
        """Steps across every attempt of the trial, in observation order, each
        tagged with its attempt, plus a combined series."""
        return self._c._get(f"/v1/trials/{trial}/steps", params={"full": int(full)})

    def network(self, trial: str) -> dict:
        """Network observed across every attempt of the trial."""
        return self._c._get(f"/v1/trials/{trial}/network")

    def cost(self, trial: str) -> dict:
        """The trial's compute charge decomposed into steps per attempt."""
        return self._c._get(f"/v1/trials/{trial}/cost")

    def timeline(self, trial: str) -> dict:
        """The trial as one ordered record across every attempt."""
        return self._c._get(f"/v1/trials/{trial}/timeline")

    def report_spend(self, trial: str, *, source: str, kind: str = "usage",
                     amount_usd: float = 0.0, tokens_in: int = 0, tokens_out: int = 0,
                     cumulative_usd: float | None = None, note: dict | None = None) -> dict:
        """Report model spend against a whole trial rather than one sandbox.

        A harness that retries knows the trial key before it knows which
        sandbox the next attempt will get, so this is the call that works
        across attempts: the signal applies to the trial's current attempt
        and any later one. Same kinds as the sandbox call: `usage`,
        `credit_exhausted`, `credit_restored`."""
        return self._c._post(f"/v1/trials/{trial}/spend",
                             {"source": source, "kind": kind, "amount_usd": amount_usd,
                              "tokens_in": tokens_in, "tokens_out": tokens_out,
                              "cumulative_usd": cumulative_usd, "note": note or {}})


    def pin(self, trial: str) -> dict:
        """Pin every checkpoint of every attempt of a trial."""
        return self._c._post(f"/v1/trials/{trial}/pin", {})

    def unpin(self, trial: str) -> dict:
        return self._c._delete(f"/v1/trials/{trial}/pin")

    def detail(self, trial: str, *, include_steps: bool = False, full: bool = False) -> dict:
        """One trial by key: {trial, attempts, cost, timeline} in one call.
        include_steps adds `steps`: every attempt's steps tagged by attempt,
        so a viewer loads once and switches attempts without another call."""
        params: dict = {}
        if include_steps:
            params["include"] = "steps"
            if full:
                params["full"] = 1
        return self._c._get(f"/v1/trials/{trial}/detail", params or None)


class Runs(_Resource):
    def list(self, *, agent: str | None = None, task: str | None = None, source: str | None = None, q: str | None = None,
             days: int = 30, limit: int = 30, offset: int = 0) -> dict:
        params: dict[str, Any] = {"days": days, "limit": limit, "offset": offset}
        for k, v in (("agent", agent), ("task", task), ("source", source), ("q", q)):
            if v: params[k] = v
        return self._c._get("/v1/runs", params=params)

    def get(self, run: str) -> dict:
        """One run by key with its trials: {run, trials}."""
        return self._c._get(f"/v1/runs/{run}")

    def set_title(self, run: str, title: str | None) -> dict:
        """A person's name for the run ("Terminal-Bench 4"); shares and pages
        show it and its URL slug. None or "" clears it. The harness's own
        run_name stays in `name`."""
        return self._c._patch(f"/v1/runs/{run}", {"title": title or ""})


class Shares(_Resource):
    """Read-only links to a run or a trial that work without credentials.
    Scope is exactly the shared object; public payloads carry no environment,
    allow lists or tokens."""

    def create(self, kind: str, key: str, *, expires_in_days: int | None = 30, title: str | None = None) -> dict:
        """kind is "run" or "trial". Returns {token, url, kind, key, title, slug, expires_at};
        the url carries the slug of the title (the share's, else the run's)."""
        body = {"kind": kind, "key": key, "expires_in_days": expires_in_days}
        if title:
            body["title"] = title
        return self._c._post("/v1/shares", body)

    def list(self) -> dict:
        return self._c._get("/v1/shares")

    def revoke(self, token: str) -> dict:
        return self._c._delete(f"/v1/shares/{token}")

    def get(self, run_key: str) -> dict | None:
        """One run by key or display name, or None."""
        page = self.list(q=run_key, limit=50)
        for row in page.get("items", []):
            if row.get("run") == run_key or row.get("name") == run_key or row.get("run_key") == run_key:
                return row
        return None


class Limits(_Resource):
    def get(self) -> dict:
        """This org's caps and live usage against them."""
        return self._c._get("/v1/limits")

    def set(self, **caps: Any) -> dict:
        """Tighten or raise your own caps (0 = unlimited within the platform
        ceiling). Keys: max_concurrent, max_vcpu, max_mem_gib, max_gpus,
        daily_budget_usd, monthly_budget_usd, max_starts_per_hour,
        max_starts_per_day, budget_mode ('hard'|'warn'), admission_wait_sec."""
        return self._c._put("/v1/limits", caps, timeout=60)


class Capacity(_Resource):
    def get(self) -> dict:
        return self._c._get("/v1/capacity")

    def reserve(self, *, vcpu: int, mem_gib: float, count: int,
                duration_minutes: int = 120,
                labels: dict[str, str] | None = None) -> dict:
        return self._c._post("/v1/reservations", {
            "vcpu": vcpu, "mem_gib": mem_gib, "count": count,
            "duration_minutes": duration_minutes, "labels": labels or {}})

    def reservations(self) -> list[dict]:
        """Reservations this org holds, each with what it costs and when it
        expires."""
        return self._c._get("/v1/reservations")

    def release(self, reservation_id: str) -> dict:
        """Release a reservation and stop paying for it.

        The SDK could create one of these and not cancel it, which is a bill a
        customer cannot stop from the client they bought with."""
        return self._c._delete(f"/v1/reservations/{reservation_id}")


class Exports(_Resource):
    @staticmethod
    def _path(export_id: str) -> str:
        if not re.fullmatch(r'exp_[A-Za-z0-9]+', export_id):
            raise ValueError('invalid export identity')
        return f'/v1/exports/{export_id}'

    def get(self, export_id: str) -> dict:
        """Return publication state, content digest and retention deadline."""
        return self._c._get(self._path(export_id))

    def download(self, export_id: str, destination: str | Path, *, max_bytes: int = 64 << 30) -> dict:
        """Write a verified tar archive. Existing files are never overwritten."""
        record = self.get(export_id)
        if record.get('state') != 'done':
            raise NuminousError('state', 'export is not available for download', 409, retryable=False)
        size, expected = record.get('size_bytes'), record.get('sha256')
        if (type(size) is not int or not 0 < size <= max_bytes or not isinstance(expected, str)
                or not re.fullmatch(r'[a-f0-9]{64}', expected)):
            raise NuminousError('provider_infra', 'invalid export metadata or download limit exceeded', 502)
        target = Path(destination)
        deadline = time.monotonic() + 1800
        with target.open('xb') as output:
            try:
                with self._c._http.stream('GET', self._path(export_id) + '/download', timeout=60) as response:
                    if response.status_code != 200:
                        response.read()
                        self._c._raise_for(response)
                        raise NuminousError('provider_infra', 'unexpected artifact response', 502)
                    if response.headers.get('content-length') != str(size):
                        raise NuminousError('provider_infra', 'artifact size differs from metadata', 502)
                    digest, count = hashlib.sha256(), 0
                    for chunk in response.iter_bytes(chunk_size=65536):
                        if time.monotonic() >= deadline:
                            raise TimeoutError('artifact download deadline exceeded')
                        count += len(chunk)
                        if count > size:
                            raise NuminousError('provider_infra', 'artifact exceeds recorded size', 502)
                        output.write(chunk)
                        digest.update(chunk)
                    if count != size or digest.hexdigest() != expected:
                        raise NuminousError('provider_infra', 'artifact download checksum mismatch', 502)
            except BaseException:
                target.unlink(missing_ok=True)
                raise
        return record | {'downloaded_to': str(target)}


class Cells:
    """Which cell this key's org lives in. Routine callers never need it:
    the client follows a cell redirect on its own."""

    def __init__(self, c: "Numinous"):
        self._c = c

    def route(self) -> dict:
        """{org, cell, local, api_url} for the caller."""
        return self._c._get("/v1/cells/route")


class GpuBatch:
    """A submitted flexible batch: jobs and a deadline, packed onto idle GPU
    seats, billed at the rate quoted when it was submitted."""

    def __init__(self, c: "Numinous", data: dict):
        self._c, self.id, self._data = c, data["id"], data

    def status(self) -> dict:
        self._data = self._c._get(f"/v1/gpu/batches/{self.id}")
        return self._data

    def job(self, job_id: str) -> dict:
        return self._c._get(f"/v1/gpu/batches/{self.id}/jobs/{job_id}")

    def wait(self, *, timeout: float = 86400.0, poll: float = 10.0) -> dict:
        """Until the batch is done, cancelled or missed. Returns its status."""
        import time as _t
        deadline = _t.monotonic() + timeout
        while _t.monotonic() < deadline:
            st = self.status()
            if st["state"] != "open":
                return st
            _t.sleep(poll)
        return self.status()

    def cancel(self) -> dict:
        return self._c._delete(f"/v1/gpu/batches/{self.id}")

    @property
    def at_risk(self) -> bool:
        return bool(self._data.get("at_risk"))


class GpuBatchBuilder:
    """`nc.gpu.batch(gpu_type="H100", deadline="24h")` then `.map(command, items)`."""

    def __init__(self, c: "Numinous", *, gpu_type: str, deadline: str, gpu_mode: str, labels: dict | None):
        self._c, self.gpu_type, self.deadline, self.gpu_mode, self.labels = c, gpu_type, deadline, gpu_mode, labels or {}

    def quote(self) -> dict:
        return self._c._get("/v1/gpu/batches/quote", params={"deadline": self.deadline})

    def map(self, command, items, *, image: str | None = None, template_id: str | None = None,
            env: dict | None = None, estimate_gpu_seconds: int = 0) -> GpuBatch:
        """One job per item. `command` is a format string with `{item}` (and
        `{i}`), or a callable(item) -> str. Each job runs in its own sandbox
        from `image` or `template_id`."""
        jobs = []
        for i, item in enumerate(items):
            cmd = command(item) if callable(command) else str(command).format(item=item, i=i)
            jobs.append({"command": cmd, "image": image, "template_id": template_id, "env": env or {},
                         "estimate_gpu_seconds": estimate_gpu_seconds})
        data = self._c._post("/v1/gpu/batches", {"gpu_type": self.gpu_type, "gpu_mode": self.gpu_mode, "deadline": self.deadline,
                                                 "jobs": jobs, "labels": self.labels}, timeout=300)
        return GpuBatch(self._c, data)

    def submit(self, jobs: list[dict]) -> GpuBatch:
        """Jobs given explicitly: [{command, image|template_id, env?, estimate_gpu_seconds?}]."""
        data = self._c._post("/v1/gpu/batches", {"gpu_type": self.gpu_type, "gpu_mode": self.gpu_mode, "deadline": self.deadline,
                                                 "jobs": jobs, "labels": self.labels}, timeout=300)
        return GpuBatch(self._c, data)


class Gpu:
    def __init__(self, c: "Numinous"):
        self._c = c

    def batch(self, *, gpu_type: str = "H100", deadline: str = "24h", gpu_mode: str = "shared",
              labels: dict | None = None) -> GpuBatchBuilder:
        """Trade time for a lower price: the longer the deadline, the lower
        the quoted rate per GPU-hour. `batch.map(run_eval, tasks)`."""
        return GpuBatchBuilder(self._c, gpu_type=gpu_type, deadline=deadline, gpu_mode=gpu_mode, labels=labels)

    def batches(self, *, state: str | None = None, limit: int = 50) -> dict:
        params: dict[str, Any] = {"limit": limit}
        if state: params["state"] = state
        return self._c._get("/v1/gpu/batches", params=params)

    def get_batch(self, batch_id: str) -> GpuBatch:
        return GpuBatch(self._c, self._c._get(f"/v1/gpu/batches/{batch_id}"))


class Numinous:
    def __init__(self, api_url: str | None = None, api_key: str | None = None):
        self.api_url = (api_url or os.environ.get(
            "NUMINOUS_API_URL", "http://127.0.0.1:8400")).rstrip("/")
        selected_key = api_key if api_key is not None else os.environ.get("NUMINOUS_API_KEY", "")
        if (not selected_key or selected_key == "nk_local_dev" or len(selected_key) > 512 or
                any(ord(character) < 33 or ord(character) > 126 for character in selected_key)):
            raise NuminousError("auth", "Configure NUMINOUS_API_KEY or pass api_key explicitly", 401)
        self.api_key = selected_key
        self._http = httpx.Client(
            base_url=self.api_url,
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=60,
        )
        self.templates = Templates(self)
        self.roms = Roms(self)
        self.snapshots = Snapshots(self)
        self.sandboxes = Sandboxes(self)
        self.exports = Exports(self)
        self.volumes = Volumes(self)
        self.usage = Usage(self)
        self.limits = Limits(self)
        self.attribution = AttributionConfig(self)
        self.trials = Trials(self)
        self.runs = Runs(self)
        self.capacity = Capacity(self)
        self.batches = Batches(self)
        self.stats = Stats(self)
        self.billing = Billing(self)
        self.labels = Labels(self)
        self.cells = Cells(self)
        self.shares = Shares(self)
        self.gpu = Gpu(self)

    def pricing(self) -> dict:
        return self._get("/v1/pricing")

    def sizes(self) -> dict:
        """The named machine sizes (vCPU, memory, GPU) a create can ask for,
        with what each costs. Naming a size is how a caller stays correct when
        the catalogue changes."""
        return self._get("/v1/sizes")

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

    def _send(self, method: str, path: str, **kw) -> Any:
        """One request, following a cell redirect once. A 421 `misdirected`
        names the cell this key's org lives in; the client re-homes to that
        origin for the rest of its life and repeats the request there. One
        hop only: a cell that bounces back is an operator problem, not a loop."""
        r = self._http.request(method, path, **kw)
        if r.status_code == 421:
            try:
                detail = r.json().get("detail") or {}
            except ValueError:
                detail = {}
            target = detail.get("api_url") if detail.get("cause") == "misdirected" else None
            if target and not getattr(self, "_rehomed", False):
                self._rehomed = True
                self.api_url = target.rstrip("/")
                self._http.base_url = self.api_url
                r = self._http.request(method, path, **kw)
        self._raise_for(r)
        return r.json()

    def _get(self, path: str, params: dict | None = None) -> Any:
        return self._send("GET", path, params=params)

    def _post(self, path: str, body: dict, timeout: float = 120) -> Any:
        return self._send("POST", path, json=body, timeout=timeout)

    def _put(self, path: str, body: dict, timeout: float = 600) -> Any:
        return self._send("PUT", path, json=body, timeout=timeout)

    def _patch(self, path: str, body: dict, timeout: float = 60) -> Any:
        return self._send("PATCH", path, json=body, timeout=timeout)

    def _delete(self, path: str) -> Any:
        return self._send("DELETE", path)
