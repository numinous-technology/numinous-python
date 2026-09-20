"""numinous — Python SDK for the Numinous Cloud sandbox API.

>>> from numinous import Numinous
>>> nc = Numinous()  # NUMINOUS_API_URL / NUMINOUS_API_KEY from env
>>> tpl = nc.templates.pack("build-env", image="ubuntu:24.04",
...                         warm_cmd="apt-get update && apt-get install -y build-essential")
>>> sb = nc.sandboxes.create(template_id=tpl["id"], vcpu=4, mem_gib=8,
...                          ttl_seconds=7200, labels={"trial_id": "tr_1"})
>>> nc.sandboxes.exec(sb["id"], "make -j test")
>>> nc.sandboxes.export(sb["id"], "/logs", to="s3://bucket/tr_1/")
>>> nc.sandboxes.destroy(sb["id"])
"""

from .client import Attribution, Numinous, NuminousError

__all__ = ["Attribution", "Numinous", "NuminousError"]

def _version() -> str:
    # One source of truth: the installed distribution's metadata, which is
    # pyproject.toml's version. A literal here fell three releases behind
    # (0.1.17 while the wheel was 0.1.20) and the CLI reported it.
    try:
        from importlib.metadata import version
        return version("numinous")
    except Exception:  # noqa: BLE001 - a source checkout that is not installed
        return "0.0.0+source"


__version__ = _version()
