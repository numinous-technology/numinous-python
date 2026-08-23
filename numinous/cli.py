"""numinous — CLI for Numinous Cloud.

Install:  curl -fsSL https://cloud.numinous.technology/install.sh | sh
Auth:     export NUMINOUS_API_URL=... NUMINOUS_API_KEY=...
"""

from __future__ import annotations

import argparse
import json
import sys

from .client import Numinous, NuminousError


def _out(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="numinous", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    tp = sub.add_parser("template", help="manage templates")
    tps = tp.add_subparsers(dest="tcmd", required=True)
    pack = tps.add_parser("pack", help="pack an environment into a template")
    pack.add_argument("--name", required=True)
    pack.add_argument("--image", help="base docker image")
    pack.add_argument("--warm", help="command to run before snapshotting")
    tps.add_parser("list", help="list templates")

    boot = sub.add_parser("boot", help="boot sandboxes from a template")
    boot.add_argument("template_id")
    boot.add_argument("--vcpu", type=int, default=2)
    boot.add_argument("--mem", type=float, default=4.0, help="GiB")
    boot.add_argument("--ttl", type=int, default=0, help="seconds; 0=default")
    boot.add_argument("--count", type=int, default=1)
    boot.add_argument("--label", action="append", default=[], help="k=v")
    boot.add_argument("--token", help="launch token (idempotency)")

    ex = sub.add_parser("exec", help="run a command in a sandbox")
    ex.add_argument("sandbox_id")
    ex.add_argument("command")
    ex.add_argument("--timeout", type=float, default=300)

    for name in ("suspend", "resume", "destroy"):
        c = sub.add_parser(name, help=f"{name} a sandbox")
        c.add_argument("sandbox_id")

    exp = sub.add_parser("export", help="export a path (works after death)")
    exp.add_argument("sandbox_id")
    exp.add_argument("path")
    exp.add_argument("--to", help="s3://bucket/prefix")

    ls = sub.add_parser("ls", help="list sandboxes")
    ls.add_argument("--label")
    ls.add_argument("--state")

    us = sub.add_parser("usage", help="usage spans by label")
    us.add_argument("--label")

    sub.add_parser("capacity", help="free capacity")
    sub.add_parser("pricing", help="current rates")

    a = p.parse_args(argv)
    nc = Numinous()
    try:
        if a.cmd == "template" and a.tcmd == "pack":
            _out(nc.templates.pack(a.name, image=a.image, warm_cmd=a.warm))
        elif a.cmd == "template" and a.tcmd == "list":
            _out(nc.templates.list())
        elif a.cmd == "boot":
            labels = dict(kv.split("=", 1) for kv in a.label)
            out = []
            for i in range(a.count):
                tok = f"{a.token}-{i}" if a.token and a.count > 1 else a.token
                out.append(nc.sandboxes.create(
                    template_id=a.template_id, vcpu=a.vcpu, mem_gib=a.mem,
                    ttl_seconds=a.ttl, labels=labels, launch_token=tok))
            _out(out if a.count > 1 else out[0])
        elif a.cmd == "exec":
            r = nc.sandboxes.exec(a.sandbox_id, a.command, timeout_sec=a.timeout)
            sys.stdout.write(r["stdout"])
            sys.stderr.write(r["stderr"])
            return r["exit_code"]
        elif a.cmd == "suspend":
            _out(nc.sandboxes.suspend(a.sandbox_id))
        elif a.cmd == "resume":
            _out(nc.sandboxes.resume(a.sandbox_id))
        elif a.cmd == "destroy":
            _out(nc.sandboxes.destroy(a.sandbox_id))
        elif a.cmd == "export":
            _out(nc.sandboxes.export(a.sandbox_id, a.path, to=a.to))
        elif a.cmd == "ls":
            _out(nc.sandboxes.list(label=a.label, state=a.state))
        elif a.cmd == "usage":
            _out(nc.usage.query(label=a.label))
        elif a.cmd == "capacity":
            _out(nc.capacity.get())
        elif a.cmd == "pricing":
            _out(nc.pricing())
        return 0
    except NuminousError as e:
        print(f"error [{e.cause}]: {e.message}", file=sys.stderr)
        # provider faults are retryable and unbilled; exit codes reflect class
        return 75 if e.is_provider_fault else 1


if __name__ == "__main__":
    raise SystemExit(main())
