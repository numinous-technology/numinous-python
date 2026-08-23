# numinous

Python SDK for [Numinous Cloud](https://cloud.numinous.technology) — build &
test environments with hard TTLs, typed failure causes,
export-that-survives-teardown, and per-second metering. Everything
non-preemptible.

## Install

```bash
pip install "numinous @ git+https://github.com/numinous-technology/numinous-python"
# PyPI release coming with GA
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
