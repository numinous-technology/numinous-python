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
    from . import __version__
    p = argparse.ArgumentParser(prog="numinous", description=__doc__)
    p.add_argument("--version", action="version", version=f"numinous {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    tp = sub.add_parser("template", help="manage templates")
    tps = tp.add_subparsers(dest="tcmd", required=True)
    pack = tps.add_parser("pack", help="pack an environment into a template")
    pack.add_argument("--name", required=True)
    pack.add_argument("--image", help="base docker image")
    pack.add_argument("--warm", help="command to run before snapshotting")
    tps.add_parser("list", help="list templates")

    boot = sub.add_parser("boot", help="boot sandboxes from a template or an image")
    boot.add_argument("template_id", nargs="?", default=None, help="template id (omit with --image)")
    boot.add_argument("--image", default=None, help="boot from a docker image instead of a template")
    boot.add_argument("--no-wait", action="store_true",
                      help="answer as soon as the create is admitted; sandboxes boot in the background "
                           "(use `numinous wait` to block on running). With --count > 1 this sends one batch request.")
    boot.add_argument("--egress", default="allow", choices=["allow", "deny", "allowlist"])
    boot.add_argument("--allow", action="append", default=[], metavar="HOST", help="allowlisted host (repeatable)")
    boot.add_argument("--rom", default=None, metavar="ROM_ID", help="mount a read-only input bundle at /rom")
    boot.add_argument("--snapshot-ttl", type=int, default=None, metavar="SECONDS",
                      help="retire replay points older than this (7 days = 604800)")
    boot.add_argument("--vcpu", type=int, default=2)
    boot.add_argument("--mem", type=float, default=4.0, help="GiB")
    boot.add_argument("--ttl", type=int, default=0, help="seconds; 0=default")
    boot.add_argument("--count", type=int, default=1)
    boot.add_argument("--label", action="append", default=[], help="k=v")
    boot.add_argument("--token", help="launch token (idempotency)")
    boot.add_argument("--auto-suspend", type=int, default=0, metavar="SECONDS",
                      help="suspend after N idle seconds; auto-wakes on exec")
    boot.add_argument("--inference-sleep", action="store_true",
                      help="opt in to the firecracker inference-sleep monitor")
    boot.add_argument("--gpu", type=str, default=None, metavar="TYPE",
                      help="GPU type (e.g. H100); routes to the GPU plane")
    boot.add_argument("--gpu-count", type=int, default=1)
    boot.add_argument("--gpu-max-hr", type=float, default=None,
                      help="per-GPU price ceiling in USD/hr")
    boot.add_argument("--snapshot-policy", default=None,
                      choices=["auto", "continuous", "per_tool", "per_exec", "manual", "off"],
                      help="replay points: auto (continuous on microVMs), per_exec, manual, off")
    boot.add_argument("--snapshot-retention", type=int, default=None,
                      help="keep the newest N replay points live")
    boot.add_argument("--volume", action="append", default=[], metavar="VOL_ID:/mount/path",
                      help="attach a persistent volume (repeatable)")

    ex = sub.add_parser("exec", help="run a command in a sandbox")
    ex.add_argument("sandbox_id")
    ex.add_argument("command")
    ex.add_argument("--timeout", type=float, default=300)
    ex.add_argument("--json", action="store_true",
                    help="print the full record (exit_code, stdout, stderr, accounting, exec_id) as JSON "
                         "instead of behaving like ssh")
    ex.add_argument("--exclusive", action="store_true",
                    help="GPU plane: take an exclusive, quiet GPU for a "
                         "perf-timed section (evicts idle residents first)")

    # GPU remoting, folded into this CLI (no separate ngpu binary): create a
    # GPU sandbox on the multiplexed plane, run a command, pull artifacts, and
    # tear down. Everything goes through the Numinous API, so no GPU-side code
    # is ever shipped in this client.
    gr = sub.add_parser("gpu", help="GPU plane helpers")
    grs = gr.add_subparsers(dest="gcmd", required=True)
    grun = grs.add_parser("run", help="run a command on a shared GPU")
    grun.add_argument("command")
    grun.add_argument("--gpu", type=str, default="H100", metavar="TYPE")
    grun.add_argument("--image", default=None, help="base docker image")
    grun.add_argument("--template", default=None, help="template id")
    grun.add_argument("--exclusive", action="store_true")
    grun.add_argument("--timeout", type=float, default=1800)
    grun.add_argument("--pull", action="append", default=[], metavar="PATH",
                      help="export this path after the run (repeatable)")
    grun.add_argument("--keep", action="store_true",
                      help="do not destroy the sandbox after the run")

    for name in ("suspend", "resume", "destroy"):
        c = sub.add_parser(name, help=f"{name} a sandbox")
        c.add_argument("sandbox_id")

    ck = sub.add_parser("checkpoint", help="freeze a running sandbox into a template")
    ck.add_argument("sandbox_id")
    ck.add_argument("--name")

    fk = sub.add_parser("fork", help="fork a live sandbox into children that start from its exact state")
    fk.add_argument("sandbox_id")
    fk.add_argument("--count", type=int, default=1)
    fk.add_argument("--ttl", type=int, default=None, help="child TTL in seconds")
    fk.add_argument("--label", action="append", default=[], metavar="K=V")

    sp = sub.add_parser("snapshot", help="take a replay point (memory + filesystem) now")
    sp.add_argument("sandbox_id")
    sp.add_argument("--label")

    mt = sub.add_parser("metrics", help="observed cpu/mem samples")
    mt.add_argument("sandbox_id")
    mt.add_argument("--limit", type=int, default=2000)
    mt.add_argument("--before", type=int, help="window.next_before from the preceding page")
    mt.add_argument("--until", help="ISO-8601 timestamp with timezone")

    st = sub.add_parser("steps", help="machine-observed steps of a sandbox (or a trial with --trial)")
    st.add_argument("id")
    st.add_argument("--trial", action="store_true", help="treat id as a trial key")
    st.add_argument("--full", action="store_true", help="include the bounded per-step record")
    st.add_argument("--series", help="comma-separated series names instead of rows (sandboxes only)")

    nw = sub.add_parser("network", help="what a sandbox (or trial with --trial) reached and what stopped it")
    nw.add_argument("id")
    nw.add_argument("--trial", action="store_true")

    eo = sub.add_parser("exec-output", help="full stdout or stderr of an exec whose response was truncated")
    eo.add_argument("sandbox_id")
    eo.add_argument("exec_id")
    eo.add_argument("--stream", choices=("stdout", "stderr"), default="stdout")
    eo.add_argument("-o", "--output", help="write to a file instead of stdout")

    exp = sub.add_parser("export", help="export a path (works after death)")
    exp.add_argument("sandbox_id")
    exp.add_argument("path")
    exp.add_argument("--to", help="s3://bucket/prefix")
    exp.add_argument("--request-token", help="reuse after a lost response to avoid a duplicate export")
    exp.add_argument("--output", help="download the published tar to a new local file")

    artifact = sub.add_parser('artifact', help='retrieve retained exports')
    artifact_sub = artifact.add_subparsers(dest='acmd', required=True)
    artifact_get = artifact_sub.add_parser('get')
    artifact_get.add_argument('export_id')
    artifact_download = artifact_sub.add_parser('download')
    artifact_download.add_argument('export_id')
    artifact_download.add_argument('destination')

    ls = sub.add_parser("ls", help="list sandboxes")
    ls.add_argument("--label")
    ls.add_argument("--state")

    us = sub.add_parser("usage", help="usage spans by label")
    us.add_argument("--label")

    vol = sub.add_parser("volume", help="manage persistent volumes")
    vs = vol.add_subparsers(dest="vcmd", required=True)
    vc = vs.add_parser("create"); vc.add_argument("--name", required=True); vc.add_argument("--size", type=float, default=10.0); vc.add_argument("--kind", choices=["sandbox","gpu"], default="sandbox")
    vs.add_parser("list")
    vd = vs.add_parser("delete"); vd.add_argument("volume_id")

    sub.add_parser("capacity", help="free capacity")
    sub.add_parser("pricing", help="current rates")

    wt = sub.add_parser("wait", help="wait until sandboxes have left `creating`")
    wt.add_argument("sandbox_ids", nargs="+")
    wt.add_argument("--timeout", type=float, default=600)

    spol = sub.add_parser("snapshot-policy", help="change how long a sandbox keeps replay points")
    spol.add_argument("sandbox_id")
    spol.add_argument("--retention", type=int, default=None, help="keep the newest N")
    spol.add_argument("--ttl", type=int, default=None, metavar="SECONDS", help="age limit; 0 clears it")

    tr = sub.add_parser("trials", help="trials (materialised from numinous.trial labels)")
    trs = tr.add_subparsers(dest="trcmd", required=True)
    trl = trs.add_parser("list"); trl.add_argument("--run", default=None); trl.add_argument("--agent", default=None)
    trl.add_argument("--state", default=None); trl.add_argument("--limit", type=int, default=50)
    trg = trs.add_parser("get"); trg.add_argument("trial_key")
    for name in ("steps", "cost", "timeline", "network"):
        x = trs.add_parser(name); x.add_argument("trial_key")

    rn = sub.add_parser("runs", help="runs (groups of trials)")
    rns = rn.add_subparsers(dest="rncmd", required=True)
    rns.add_parser("list").add_argument("--limit", type=int, default=50)
    rns.add_parser("get").add_argument("run_key")

    rom = sub.add_parser("rom", help="read-only input bundles mounted at /rom")
    roms = rom.add_subparsers(dest="rcmd", required=True)
    rc = roms.add_parser("create"); rc.add_argument("--name", required=True)
    rc.add_argument("--file", action="append", default=[], metavar="PATH=LOCALFILE",
                    help="path inside /rom and the local file to read (repeatable)")
    roms.add_parser("list")

    sk = sub.add_parser("skill", help="print the skill file an agent reads to drive this CLI")
    sk.add_argument("--install", default=None, metavar="DIR", help="also write it to DIR/SKILL.md")

    sub.add_parser("whoami", help="which organisation this key belongs to")

    a = p.parse_args(argv)
    if a.cmd == "skill":
        # Documentation needs no key: an agent reads this before it has one.
        from .skill import SKILL
        if a.install:
            import os
            os.makedirs(a.install, exist_ok=True)
            with open(os.path.join(a.install, "SKILL.md"), "w") as fh:
                fh.write(SKILL)
            print(os.path.join(a.install, "SKILL.md"))
        else:
            print(SKILL)
        return 0
    try:
        nc = Numinous()
        if a.cmd == "template" and a.tcmd == "pack":
            _out(nc.templates.pack(a.name, image=a.image, warm_cmd=a.warm))
        elif a.cmd == "template" and a.tcmd == "list":
            _out(nc.templates.list())
        elif a.cmd == "boot":
            if not a.template_id and not a.image:
                raise ValueError("give a template id or --image")
            labels = dict(kv.split("=", 1) for kv in a.label)
            volumes = [{"volume_id": v.split(":", 1)[0], "mount_path": v.split(":", 1)[1]} for v in a.volume] or None
            common = dict(template_id=a.template_id, image=a.image, rom_id=a.rom, vcpu=a.vcpu, mem_gib=a.mem,
                          egress=a.egress, allow=a.allow or None,
                          ttl_seconds=a.ttl, labels=labels, auto_suspend_idle_seconds=a.auto_suspend,
                          inference_sleep=a.inference_sleep, gpu=(a.gpu_count if a.gpu else 0), gpu_type=a.gpu,
                          gpu_max_hr=a.gpu_max_hr, snapshot_policy=a.snapshot_policy,
                          snapshot_retention=a.snapshot_retention, snapshot_ttl_seconds=a.snapshot_ttl,
                          volumes=volumes)
            if a.no_wait and a.count > 1:
                # one request admits the whole fan-out; boots run in the background
                specs = [dict(common, launch_token=(f"{a.token}-{i}" if a.token else None)) for i in range(a.count)]
                _out(nc.sandboxes.create_many(specs))
            else:
                out = []
                for i in range(a.count):
                    tok = f"{a.token}-{i}" if a.token and a.count > 1 else a.token
                    out.append(nc.sandboxes.create(launch_token=tok, wait=not a.no_wait, **common))
                _out(out if a.count > 1 else out[0])
        elif a.cmd == "wait":
            _out(nc.sandboxes.wait_running(a.sandbox_ids, timeout=a.timeout))
        elif a.cmd == "snapshot-policy":
            _out(nc.sandboxes.snapshot_policy(a.sandbox_id, retention=a.retention, ttl_seconds=a.ttl))
        elif a.cmd == "trials":
            if a.trcmd == "list":
                filters = {k: v for k, v in (("agent", a.agent), ("state", a.state)) if v}
                _out(nc.trials.list(run=a.run, filters=filters or None, limit=a.limit))
            elif a.trcmd == "get":
                _out(nc.trials.get(a.trial_key))
            else:
                _out(getattr(nc.trials, a.trcmd)(a.trial_key))
        elif a.cmd == "runs":
            _out(nc.runs.list(limit=a.limit) if a.rncmd == "list" else nc.runs.get(a.run_key))
        elif a.cmd == "rom":
            if a.rcmd == "create":
                files = {}
                for spec in a.file:
                    inside, local = spec.split("=", 1)
                    with open(local) as fh:
                        files[inside] = fh.read()
                _out(nc.roms.create(a.name, files))
            else:
                _out(nc.roms.list())
        elif a.cmd == "whoami":
            _out(nc.whoami())
        elif a.cmd == "exec":
            env = {"GPU_EXCLUSIVE": "1"} if a.exclusive else {}
            r = nc.sandboxes.exec(a.sandbox_id, a.command, timeout_sec=a.timeout,
                                  env=env)
            if a.json:
                _out(r)
                return 0
            sys.stdout.write(r["stdout"])
            sys.stderr.write(r["stderr"])
            return r["exit_code"]
        elif a.cmd == "gpu" and a.gcmd == "run":
            sb = nc.sandboxes.create(
                template_id=a.template, image=a.image, gpu=1, gpu_type=a.gpu)
            sid = sb["id"]
            try:
                env = {"GPU_EXCLUSIVE": "1"} if a.exclusive else {}
                r = nc.sandboxes.exec(sid, a.command, timeout_sec=a.timeout,
                                      env=env)
                sys.stdout.write(r.get("stdout", ""))
                sys.stderr.write(r.get("stderr", ""))
                for pth in a.pull:
                    _out(nc.sandboxes.export(sid, pth))
                return r["exit_code"]
            finally:
                if not a.keep:
                    nc.sandboxes.destroy(sid)
        elif a.cmd == "suspend":
            _out(nc.sandboxes.suspend(a.sandbox_id))
        elif a.cmd == "resume":
            _out(nc.sandboxes.resume(a.sandbox_id))
        elif a.cmd == "destroy":
            _out(nc.sandboxes.destroy(a.sandbox_id))
        elif a.cmd == "checkpoint":
            _out(nc.sandboxes.checkpoint(a.sandbox_id, name=a.name))
        elif a.cmd == "fork":
            labels = dict(kv.split("=", 1) for kv in a.label if "=" in kv)
            _out(nc.sandboxes.fork(a.sandbox_id, count=a.count, ttl_seconds=a.ttl, labels=labels))
        elif a.cmd == "snapshot":
            _out(nc.sandboxes.snapshot(a.sandbox_id, label=a.label))
        elif a.cmd == "metrics":
            _out(nc.sandboxes.metrics(a.sandbox_id, limit=a.limit, before=a.before, until=a.until))
        elif a.cmd == "steps":
            if a.trial:
                _out(nc.trials.steps(a.id, full=a.full))
            elif a.series:
                _out(nc.sandboxes.steps_series(a.id, [f.strip() for f in a.series.split(",")]))
            else:
                _out(nc.sandboxes.steps(a.id, full=a.full))
        elif a.cmd == "network":
            _out(nc.trials.network(a.id) if a.trial else nc.sandboxes.network(a.id))
        elif a.cmd == "exec-output":
            data = nc.sandboxes.exec_output(a.sandbox_id, a.exec_id, stream=a.stream)
            if a.output:
                with open(a.output, "wb") as f:
                    f.write(data)
                _out({"written": a.output, "bytes": len(data)})
            else:
                sys.stdout.buffer.write(data)
        elif a.cmd == "export":
            result = nc.sandboxes.export(a.sandbox_id, a.path, to=a.to, request_token=a.request_token)
            if a.output:
                result = nc.exports.download(result['export_id'], a.output)
            _out(result)
        elif a.cmd == 'artifact' and a.acmd == 'get':
            _out(nc.exports.get(a.export_id))
        elif a.cmd == 'artifact' and a.acmd == 'download':
            _out(nc.exports.download(a.export_id, a.destination))
        elif a.cmd == "ls":
            _out(nc.sandboxes.list(label=a.label, state=a.state))
        elif a.cmd == "usage":
            _out(nc.usage.query(label=a.label))
        elif a.cmd == "volume" and a.vcmd == "create":
            _out(nc.volumes.create(a.name, size_gib=a.size, kind=a.kind))
        elif a.cmd == "volume" and a.vcmd == "list":
            _out(nc.volumes.list())
        elif a.cmd == "volume" and a.vcmd == "delete":
            _out(nc.volumes.delete(a.volume_id))
        elif a.cmd == "capacity":
            _out(nc.capacity.get())
        elif a.cmd == "pricing":
            _out(nc.pricing())
        return 0
    except NuminousError as e:
        print(f"error [{e.cause}]: {e.message}", file=sys.stderr)
        # provider faults are retryable and unbilled; exit codes reflect class
        return 75 if e.is_provider_fault else 1
    except (OSError, ValueError) as e:
        print(f'error: {e}', file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
