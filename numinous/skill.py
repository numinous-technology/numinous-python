"""The skill file an agent reads to operate Numinous Cloud through this CLI."""

SKILL = r'''---
name: numinous-cloud
description: Run, checkpoint, fork and inspect sandboxes on Numinous Cloud from the command line. Use for launching trials, fan-outs, GPU work, reading machine-observed steps, and replay.
---

# Numinous Cloud, headless

Everything the platform does is reachable from `numinous` with a key. Every
command prints JSON, except `exec`, which behaves like `ssh`: the command's
stdout to stdout, its stderr to stderr, its exit code as the exit code (add
`--json` for the full record). Nothing here needs the dashboard; billing
(buying credit) is the one thing that does. `numinous --version` prints the
CLI version.

## Setup

```
export NUMINOUS_API_URL=https://api.cloud.numinous.technology
export NUMINOUS_API_KEY=nk_live_...
numinous whoami            # confirms the key and the organisation
```

## The unit of work is a sandbox

A sandbox is a microVM with a filesystem and, on the Firecracker plane, its
memory too. It boots from a template (an image the platform has already
prepared) or straight from a Docker image.

```
numinous template pack --name my-env --image python:3.12-slim
numinous template list
numinous boot <template_id> --vcpu 2 --mem 4 --ttl 3600
numinous boot --image alpine:3.20 --ttl 600
numinous exec <sandbox_id> 'python -c "print(1)"'
numinous destroy <sandbox_id>
```

`exec` pipes like `ssh` and exits with the command's exit code. With
`--json` it prints the record instead: `exit_code`, `stdout`, `stderr`,
`exec_id`, `accounting` (what the command's process tree actually did) and, if
the output was cut, a pointer to the full copy
(`numinous exec-output <sandbox_id> <exec_id>`).

## Fan-out: many sandboxes at once

Do not open a thousand connections. Admit the whole batch in one request and
wait for readiness separately:

```
numinous boot <template_id> --count 200 --no-wait --label numinous.run=my-sweep
numinous wait <id> <id> ... --timeout 600
```

`--no-wait` answers as soon as each create is admitted (state `creating`);
the sandbox reaches `running`, or a typed failure written on it, moments
later. A refusal (quota, credit, capacity) is reported per item with a
`cause`; the rest of the batch is unaffected.

## Checkpoints, replay and forks

Every sandbox is checkpointed continuously at the machine's own step
boundaries (each process exit that changed something), not on a timer. A
checkpoint is memory and disk together, so any point can be restored or
forked.

```
numinous snapshot <sandbox_id> --label before-fix     # an explicit point
numinous steps <sandbox_id>                           # what the machine did, step by step
numinous steps <sandbox_id> --full                    # with files changed per step
numinous checkpoint <sandbox_id> --name my-env-v2     # freeze into a template
numinous fork <sandbox_id> --count 4 --ttl 1800       # children from its exact state
```

A fork starts from the parent's exact memory and disk; the parent keeps
running. Use it to branch an agent at a decision point, or to fan a prepared
environment out without booting from scratch.

Retention: keep the newest N points (`--snapshot-retention 50`) and, or
instead, drop points older than an age (`--snapshot-ttl 604800` for seven
days). Change later with `numinous snapshot-policy <id> --ttl 86400`.

## Read-only inputs (ROM)

Inputs every trial should see identically, stored once per machine and
mounted read-only at `/rom`:

```
numinous rom create --name fixtures --file data/train.csv=./train.csv
numinous boot <template_id> --rom <rom_id>
```

## Egress

`--egress deny` (nothing), `--egress allowlist --allow pypi.org --allow
github.com` (named hosts only, enforced at the resolver and proxy), or
`allow`. Use `deny` or an allowlist for anything that is being graded.

## Trials and runs (attribution)

Label a sandbox `numinous.run=<run>` and `numinous.trial=<trial>` (add
`numinous.agent`, `numinous.task`) and the platform rolls it up:

```
numinous trials list --run my-sweep
numinous trials get <trial_key>          # attempts, reward, checkpoints, cost
numinous trials steps <trial_key>
numinous runs list
```

## GPU

```
numinous boot <template_id> --gpu H100 --gpu-count 1
numinous gpu run <template_id> 'python train.py'      # boot, run, export, destroy
```

Shared cards (`gpu_mode=shared`) let several trials use one card; the
platform bills the fraction actually held.

## Volumes

Durable, single-attach, flushed before teardown is acknowledged:

```
numinous volume create --name scratch --size 20
numinous boot <template_id> --volume <vol_id>:/data
```

## Errors

Every error is typed: `cause` says what happened, `retryable` says whether
retrying can help. `provider_*` causes are the platform's fault and are never
billed. Exit code 75 means "provider fault, retry"; 1 means "your request".

## What to check when something is slow or missing

```
numinous capacity          # free vCPU/memory in the pool
numinous usage             # spend by label
numinous pricing           # rates
```
'''
