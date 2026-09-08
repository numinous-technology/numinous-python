# numinous

Python SDK for [Numinous Cloud](https://cloud.numinous.technology) — build &
test environments with hard TTLs, typed failure causes,
export-that-survives-teardown, and per-second metering. Everything
non-preemptible.

## Install

```bash
pip install numinous            # SDK + CLI from PyPI
# or:
curl -fsSL https://cloud.numinous.technology/install.sh | sh
```

## Use

```python
from numinous import Numinous

nc = Numinous()  # NUMINOUS_API_URL / NUMINOUS_API_KEY from env

tpl = nc.templates.pack("build-env", image="ubuntu:24.04",
                        warm_cmd="apt-get update && apt-get install -y build-essential")

sb = nc.sandboxes.create(template_id=tpl["id"], vcpu=4, mem_gib=8,
                         ttl_seconds=7200, launch_token="job-1-attempt-1",
                         labels={"trial_id": "tr_1"})

nc.sandboxes.exec(sb["id"], "make -j test")
nc.sandboxes.export(sb["id"], "/logs", to="s3://bucket/tr_1/")  # works after death too
out = nc.sandboxes.destroy(sb["id"])
out["teardown_proof"]                      # {"verified_absent": true, ...}
nc.usage.query(label="trial_id:tr_1")      # per-second spans, typed unbilled faults
```

Replay points and the machine's own record of a sandbox:

```python
sb = nc.sandboxes.create(image="python:3.12-slim", run_mode="docker", ttl_seconds=1800)
nc.sandboxes.exec(sb["id"], "pip install requests && python -c 'import requests'")
nc.sandboxes.snapshot(sb["id"], label="deps installed")   # a replay point now
steps = nc.sandboxes.steps(sb["id"], full=True)           # one row per machine-observed boundary:
                                                          # command as issued, exit code, bytes, files, flows
nc.sandboxes.network(sb["id"])                            # remotes reached, denials, policy proofs
nc.sandboxes.steps_diff(sb["id"])                         # files changed across the attempt
nc.sandboxes.cost(sb["id"])                               # what each step cost, balanced to the charge
nc.sandboxes.timeline(sb["id"])                           # everything recorded, in order
```

Persistent volumes (Firecracker plane: one sandbox at a time, flushed to
durable storage before that sandbox's teardown completes):

```python
vol = nc.volumes.create("model-cache", size_gib=20)       # idempotent by name
sb = nc.sandboxes.create(image="python:3.12-slim", volumes=[{"volume_id": vol["id"], "mount_path": "/cache"}])
nc.volumes.get(vol["id"])                                 # attached_sandbox_id, attach_generation, last flush
```

## Typed errors

```python
from numinous import Numinous, NuminousError

try:
    nc.sandboxes.create(...)
except NuminousError as e:
    e.cause              # "provider_capacity" | "user_image_build_failed" | ...
    e.is_provider_fault  # provider_* causes are never billed
    e.retryable
```

API reference: the control plane serves its own OpenAPI spec at
`GET /openapi.json`.
