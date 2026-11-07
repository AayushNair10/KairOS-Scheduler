# KairOS

`scx_kairos` is a Linux CPU scheduler, written in C with
[sched_ext](https://docs.kernel.org/scheduler/sched-ext.html) (eBPF). It
targets a latency-critical (LC) service that shares CPUs with batch work.
The aim is low request latency for the service, while batch work keeps
most of its throughput.

kairos is not an operating system. It is a scheduling policy that runs
inside an ordinary Linux kernel through sched_ext. Linux still does
everything else. kairos only decides which runnable thread runs on which
CPU, and when. If kairos stops, Linux goes back to its own scheduler.

## How it works

### Words used here

- **LC (latency-critical) task:** a thread someone is waiting on, e.g. a
  server thread answering a request. What counts is its response time.
- **Batch task:** background work that nobody waits on moment to moment,
  e.g. compressing files. What counts is its throughput.
- **Class:** whether kairos treats a task as LC or batch. A task is batch
  if it runs under Linux's `SCHED_BATCH` or `SCHED_IDLE` policy (start it
  with `chrt --batch 0 CMD`); everything else is LC (`--class=policy`).
  The class comes from that label, and kairos never changes it.
- **Pools:** the CPUs kairos manages are split into LC CPUs and batch
  CPUs. Each pool has one queue shared by all its CPUs.
- **Borrower:** a batch task running on an LC CPU, because no LC task
  needed that CPU.
- **Preempting:** stopping a running task before its time slice ends, so
  another task can run.

### What happens to a task

1. **It wakes up**, e.g. a server thread because a request arrived. If
   a CPU is idle, the task runs there at once. kairos looks in the
   task's own pool first, then in the other pool. Within each, it tries
   the task's previous CPU first, whose cache may still hold its data.
2. **No CPU is idle, and the task is LC.** If a borrower is running on
   an LC CPU, kairos stops it and gives the CPU to the LC task. The
   borrower goes back to the batch queue. Two refinements:
   - **The 1 ms wait** (`--preempt-wait-us=1000
     --preempt-min-run-us=100`). Most wakeups come from runtime threads
     that run for about 10 µs. If a task's runs average under 100 µs, it
     first waits up to 1 ms for a CPU to free up on its own. Only then is
     a borrower stopped. Request workers, whose runs are longer, still
     stop a borrower at once. This cuts preemptions by about 90% and
     gives batch work more CPU.
   - **One preemption per borrower** (`--claim-borrower`). Two LC tasks
     that wake at the same moment stop two different borrowers, not the
     same one twice.
3. **Otherwise the task waits** in its pool's shared queue. Batch tasks
   never preempt anyone.
4. **A CPU that needs work** takes the next task from its own pool's
   queue. If that queue is empty, it takes one from the other pool's
   queue: it lends itself. No CPU stays idle while either queue holds
   work. A borrower gets a short slice, so the CPU comes back soon.
5. **The starvation guard** (`--max-wait-ms=2000`). A task that has
   waited 2 s runs before the CPU's own queue. That bounds how long it
   waits, not how much CPU it gets.
6. **Pool sizing.** A timer measures the CPU that LC tasks use and
   need. It makes the LC pool that much plus a buffer (1.5 CPUs),
   rounded up. On
   the 3-CPU test machine, the pool stayed at all 3 CPUs, so batch work
   only ever borrowed.

The policy is `scheduler/scx_kairos.bpf.c`; its top comment describes
every part. The loader is `scheduler/scx_kairos.c`.

### kairos and Linux compared

| | Linux's default (EEVDF) | kairos |
|---|---|---|
| Queues | One per CPU | One per class, shared by all CPUs of its pool |
| Idle CPUs | A woken task is placed on an idle CPU if one is found. Idle CPUs pull tasks from busy ones, and periodic load balancing moves more | Take the next task from a shared queue: their own pool's, else the other's |
| How CPU time is divided | By weight. Over time every task gets its share; a woken task preempts if its virtual deadline is earlier | By class. On LC CPUs, LC tasks always go first; batch gets what is left |
| `SCHED_BATCH` tasks | The same share as normal tasks; they just don't preempt others on wakeup | Batch: they borrow CPUs the LC tasks leave idle |
| `SCHED_IDLE` tasks | A tiny share (weight 3 against 1,024). A woken normal task preempts them at once, and a CPU running only them counts as idle | Batch, as above |
| Reserving CPUs | cpusets: a hard split, so unused reserved CPUs stay idle | A soft split: unused LC CPUs are lent to batch work and taken back when needed |
| Delayed preemption | No equivalent | Short-running LC tasks wait up to 1 ms before a borrower is stopped |

### What each part contributes (measured; section numbers in `docs/RESEARCH_LOG.md`)

- **Preempting only batch tasks.** On the original single-queue design
  at 3,500 req/s, preempting only batch tasks gave a 1.0 ms median.
  Never preempting, with 3 ms slices, gave 3.5 ms (E1c, 6.5). Preempting request threads
  too hurt latency at every load, because a request gets paused halfway
  (E1b, 6.4).
- **The shared LC queue.** With a queue per CPU instead, the busy median
  rose from 0.86–1.04 ms to 1.46–1.71 ms (E12, 6.24; 3 reps).
- **The 1 ms wait.** About 90% fewer preemptions (E11, 6.23) and 10%
  more batch throughput, for a 28% higher busy median (F, 6.32).
- **One preemption per borrower.** Busy median −2% for kairos; no
  significant difference for kairos-pw1000r100 (E14, 6.30).
- **Classification by label.** Classifying tasks by how long they run,
  instead of by label, more than doubled the busy median, for 7% more
  batch (E5, 6.38).
- **Pool sizing.** No measurable effect on 3 CPUs (6.32, E15 in 6.37).

## Design

### The parts

```
 user space                         kernel
+-----------------------+          +---------------------------------------------+
| scx_kairos (loader)   |  loads   | sched_ext core                              |
|  - options -> BPF     |--------->|  calls kairos's callbacks when a task wakes,|
|    constants          |          |  becomes runnable, starts or stops running, |
|  - prints counters    |<---------|  and when a CPU needs work                  |
|    every second       |   maps   |                                             |
+-----------------------+          | scx_kairos.bpf.c (policy)                   |
                                   |  callbacks + a 1 ms timer + a wait timer    |
                                   |                                             |
                                   |  LC_DSQ (LC tasks)  BATCH_DSQ (batch tasks) |
                                   |         \              /                    |
                                   |  dispatch() moves the next task into a      |
                                   |  CPU's local queue; the CPU runs it         |
                                   |  [CPU 0 local] [CPU 1 local] [CPU 2 local]  |
                                   +---------------------------------------------+
```

- **The policy** (`scheduler/scx_kairos.bpf.c`, C compiled to BPF) runs
  inside the kernel. sched_ext calls it on scheduling events, and it
  decides where each task goes. It started as a copy of the kernel's
  `scx_simple` example.
- **The loader** (`scheduler/scx_kairos.c`, user space) does three
  things:
  - Before loading, it writes the command-line options into the
    policy's constants. The BPF verifier then sees them as fixed values
    and removes code for features that are off.
  - It marks which CPUs belong to the split.
  - Once a second it prints the policy's counters (optionally as CSV
    with `--stats`). Ctrl-C unloads the policy.
- **If the policy misbehaves**, e.g. a task waits 30 s, the kernel
  unloads it and goes back to EEVDF.

### Queues and state

- **Queues** (sched_ext *dispatch queues*):
  - `LC_DSQ` and `BATCH_DSQ` are kairos's two shared queues, one per
    class, used by every CPU.
  - Each CPU also has its own *local* queue, which the kernel runs from.
    Putting a task there means it runs on that CPU next.
- **Order within a queue: virtual time,** as in `scx_simple`.
  - A task's virtual time grows with the CPU time it uses, divided by
    its weight (nice level).
  - A task that slept gets back at most 20 ms of credit.
  - So within a class, tasks share CPU time fairly.
- **Per task** (BPF task storage, `struct task_ctx`):
  - when it became ready to run, for wait times and the starvation
    guard;
  - when it started running, to charge its CPU time;
  - `avg_run`: a moving average of how long it runs each time it
    wakes, for the 1 ms rule;
  - whether it is delayed, waiting for the wait timer.
- **Per CPU** (`struct cpu_ctx`):
  - whether the running task is batch, i.e. whether a task on an LC CPU
    is a borrower, and when it started;
  - `preempt_pending`: set once a wakeup has chosen to preempt this
    CPU, cleared when the CPU switches tasks.
- **Global:**
  - `nr_lc`, the LC pool's size;
  - `lc_demand`, the measured LC demand in CPUs × 1024 (BPF has no
    floating point);
  - the counters the loader prints.

### Callbacks

| Callback | When the kernel calls it | What kairos does |
|---|---|---|
| `select_cpu` | A task wakes up | Looks for an idle CPU: own pool first, then the other, the task's previous CPU first within each. If one is found, the task goes into that CPU's local queue and runs at once |
| `enqueue` | A runnable task needs a place: no idle CPU was found, or it was preempted or its slice ran out | A woken LC task preempts a borrower (at once, or after the 1 ms wait). Otherwise the task goes into `LC_DSQ` or `BATCH_DSQ` by class |
| `dispatch` | A CPU has nothing in its local queue | LC CPU: an overdue batch task first (guard), then `LC_DSQ`, then `BATCH_DSQ`. Batch CPU: an overdue LC task, then `BATCH_DSQ`, then `LC_DSQ` |
| `runnable` | A task becomes ready to run | Notes the time, to measure its wait |
| `running` | A task starts on a CPU | Records its wait. Notes in `cpu_ctx` whether a borrower now runs here. A batch task starting on an LC CPU gets its slice cut to the 3 ms borrow slice |
| `stopping` | A task stops running | Charges its CPU time (virtual time, LC demand). If it went to sleep, updates `avg_run`. Clears the CPU's `preempt_pending` |
| `quiescent`, `enable`, `init_task` | A task sleeps, joins or is created | Counting; initial virtual time; task storage |
| `init`, `exit` | Load, unload | Creates the queues and starts the 1 ms timer; records why it exited |

### How each mechanism is implemented

- **Classification.** `task_batch()` returns true if the task's policy is
  `SCHED_BATCH` or `SCHED_IDLE`. It is checked at each decision, so a
  task changed with `chrt` while running moves class with its next
  decision.
- **Preempting a borrower** (in `enqueue`):
  - It scans the LC CPUs for one whose `cpu_ctx` says a batch task is
    running there. It takes the first in CPU order.
  - It inserts the woken LC task into that CPU's local queue with
    `SCX_ENQ_PREEMPT`. The kernel puts the task first and sets the
    borrower's remaining slice to 0.
  - So the borrower stops at once and returns through `enqueue` into
    `BATCH_DSQ`.
  - Batch tasks on batch CPUs are never preempted.
- **One preemption per borrower** (`--claim-borrower`, `claim_victim()`).
  - The scan skips CPUs with `preempt_pending` set.
  - A wakeup sets the flag with an atomic compare-and-swap from 0 to 1.
    Of two wakeups racing for the same CPU, only one wins.
  - The loser takes another borrower, or waits in `LC_DSQ`.
- **The 1 ms wait** (`--preempt-wait-us`, `--preempt-min-run-us`).
  - **Which tasks preempt at once:** a woken LC task whose `avg_run`
    is at least 100 µs.
  - **The others** go into `LC_DSQ` marked *delayed*. Any LC CPU that
    frees up in the meantime takes them, and a one-shot timer is armed
    for 1 ms.
  - **When the timer fires** (`wait_timerfn()`), it finds the first
    delayed task that has waited 1 ms. It kicks one borrower's CPU with
    `SCX_KICK_PREEMPT`, and that CPU then runs `dispatch()`, which takes
    `LC_DSQ` first.
  - It re-arms itself for the next delayed task.
- **Lending.**
  - An LC CPU with nothing in `LC_DSQ` takes a task from `BATCH_DSQ` in
    `dispatch()`.
  - `running()` shortens that task's slice to 3 ms, so the CPU comes
    back soon even without a preemption.
  - Slices: LC tasks 3 ms, batch tasks on batch CPUs 20 ms.
- **Starvation guard** (`--max-wait-ms`, `run_overdue()`).
  - Before a CPU takes from its own queue, `dispatch()` peeks at the
    other queue's first task.
  - If that task has waited longer than the limit, it runs first.
  - The peek is cheap and lock-free, and the move happens only when
    something is overdue.
- **Pool sizing** (`update_split()`, every 1 ms tick).
  - **One sample:** the CPU time LC tasks used in the tick, in CPUs,
    plus one CPU for each LC task still waiting.
  - **Demand:** a moving average of the samples over about 64 ms.
  - **Pool size:** demand plus the buffer, rounded up, between
    `--lc-min` and the number of split CPUs.
  - **Shrinking** happens only if it would still happen with 0.25 CPU
    more demand, so the pool doesn't flip back and forth.
  - **Which CPUs are LC CPUs:** the first `nr_lc` split CPUs in CPU
    order. CPUs outside `--split-cpus` act as LC CPUs.
- **BPF constraints.** The verifier only accepts code it can prove safe.
  So:
  - every loop is bounded (`bpf_for`);
  - every map and task-storage lookup is null-checked;
  - all arithmetic is integer (CPUs × 1024, nanoseconds).

The source also keeps the first design (`--adaptive` mode switching,
`--force-preempt`, `--never-preempt`) and the options used only in
experiments and ablations. All are off by default, and `--help` marks
them.

## Results

These come from a 4-vCPU VM (Apple M2 host, Linux 7.0):
- on CPUs 0–2, a Go gRPC server (16 worker threads, 500 µs of CPU work
  per request) and, as the batch work, 2 single-threaded zstd jobs
  compressing the same 64 MB file over and over;
- an open-loop load generator ran on CPU 3;
- 10 interleaved reps per configuration;
- tests: Wilcoxon signed-rank paired by rep, with Holm correction.

`docs/RESEARCH_LOG.md` (section 6.32) has the full results and the
rules set before the runs.

![Latency against batch throughput per scheduler](docs/report/figures/tradeoff.png)

Medians over 10 reps; whiskers show the interquartile range. Up and to
the left is better.

**Burst sweep** (busy 3,500 req/s and quiet 500 req/s phases, 10 ms to
10 s long). Medians:

| scheduler | busy p50 (ms) | busy requests over 5 ms | batch (MB/s) |
|---|---|---|---|
| kairos (no wait) | 1.10 | 7.6% | 324 |
| **kairos-pw1000r100** (1 ms wait) | 1.41 | 9.2% | 355 |
| scx_bpfland | 1.58 | 16.2% | 360 |
| EEVDF, batch as SCHED_IDLE | 2.37 | 24.0% | 341 |
| EEVDF (Linux default) | 4.11 | 43.3% | 365 |
| scx_lavd | 8.62 | 73.4% | 386 |

- **kairos-pw1000r100 is the main configuration.**
  - Against scx_bpfland it has lower busy p50 in 10/10 reps (Holm
    p = 0.016), and no difference in batch (p = 0.92).
  - The starvation guard was added to it later (`--main-config`). It
    never fired in the burst sweep and made no difference at p < 0.05
    (section 6.36).
- **Without the wait** (the first row), kairos has the lowest latency
  in the table, for about 9% less batch.
- **On a replay of Azure Functions traffic** the top three keep their
  order: p50 0.87 ms for kairos, 1.02 for kairos-pw1000r100 and 1.14 for
  bpfland. EEVDF is last, at 1.66 ms.

Ideas that didn't pay on this machine are in the log too (section 6):
- a dependency rule for the wait;
- sizing the LC pool more tightly;
- classifying tasks by run time instead of policy;
- an ML predictor of LC demand.

## Requirements

- **A kernel with sched_ext** (`CONFIG_SCHED_CLASS_EXT=y`). Developed
  and tested only on Linux 7.0 (Ubuntu 26.04, arm64).
  - kfunc names change between kernel versions. The code follows the
    `tools/sched_ext` examples of 7.0, so older kernels may need changes.
  - Check: `cat /sys/kernel/sched_ext/state`.
- **The kernel source for the running kernel,** e.g. in `~/linux-src`.
  The build uses its `tools/sched_ext` headers, libbpf and bpftool.
- **Packages (Ubuntu):** `clang llvm llvm-dev libelf-dev zlib1g-dev
  libssl-dev libcap-dev binutils-dev pkg-config make`.
- **For the workloads:** Go ≥ 1.26 (`golang-go`), `zstd`, and for the
  Redis variant `redis-server`.
- **Python 3.** The analysis scripts use only the standard library.
- **Optional, for the baselines:** `scx_bpfland` and `scx_lavd` from
  [sched-ext/scx](https://github.com/sched-ext/scx), built with cargo
  (`experiments/run.sh` looks in `~/scx/target/release`).

## Build

```sh
# 1. The kernel's sched_ext examples, once. This produces libbpf, bpftool
#    and vmlinux.h for the running kernel. Repeat it after a kernel update.
make -C ~/linux-src/tools/sched_ext

# 2. The scheduler (set KSRC=... if the kernel source is elsewhere).
make -C scheduler

# 3. The workloads (gRPC server and load generator, into workloads/bin/).
make -C workloads

# 4. The batch jobs' input: 64 MB of a tar of the kernel source, once.
./workloads/batch/make_input.sh      # -> ~/kairos-data/zstd-input-64M.tar
```

## Run the scheduler

```sh
sudo ./scheduler/scx_kairos --main-config
```

- **`--main-config`** is the configuration the evaluation chose. It
  expands to:

  ```
  --split --class=policy --lc-buffer-pct=150 --claim-borrower
  --preempt-wait-us=1000 --preempt-min-run-us=100 --max-wait-ms=2000
  ```

  Options after it override it.
- **Batch work must be labelled.** Start it with `chrt --batch 0 CMD` or
  `chrt --idle 0 CMD`. Everything else is treated as latency-critical.
- **`--split-cpus LIST`** limits the scheduler to some CPUs. The
  experiments use `--split-cpus=0-2` because the load generator has
  CPU 3.
- **Output.** The loader prints its counters once a second.
  `--stats FILE` also writes them as CSV.
- **Unloading.** Ctrl-C unloads the scheduler and the kernel goes back
  to its default. If the scheduler misbehaves, the sched_ext watchdog
  unloads it; `dmesg` shows why.
- **Is it running?** `cat /sys/kernel/sched_ext/state` and
  `cat /sys/kernel/sched_ext/root/ops`.
- **All options:** `./scheduler/scx_kairos --help`. The options for
  experiments and ablations only are marked there.

## Run the experiments

Each experiment is a config file in `experiments/configs/`.
`experiments/run.sh` runs each config file:
- every scheduler in it, once per rep, with the order reversed every
  other rep;
- before each run, it starts the server, the batch jobs and the load
  generator;
- each run's raw data goes to `results/<date>-<name>/<scheduler>-r<rep>/`,
  with a `meta.json` (git commit, kernel, options).

It loads schedulers with `sudo`. Keep the machine otherwise idle, and
keep a laptop host awake and plugged in (e.g. `caffeinate` on macOS).

```sh
./experiments/run.sh experiments/configs/smoke.conf     # a quick check
./experiments/final.sh                                  # the final evaluation, ~7 h
```

`results/` isn't in git (about 18 GB here). The numbers are in
`docs/RESEARCH_LOG.md`.

## Analyse

```sh
./analysis/summarize_run.py results/<date>-<name>/<run>          # one run
./analysis/checks.py results/<date>-<name>                        # run checks, service CPU per request
./analysis/stats.py results/<date>-<name> --ref kairos            # tests against a reference
./analysis/byburst.py results/<date>-<name> --ref kairos          # burst sweep, per burst length and time since a burst started/ended
./analysis/bylevel.py results/<date>-<name> --ref kairos          # Azure replay, per load level
```

The report's figures need matplotlib, which the other scripts don't:

```sh
python3 -m venv ~/kairos-data/venv && ~/kairos-data/venv/bin/pip install matplotlib
~/kairos-data/venv/bin/python analysis/plots.py --png   # -> docs/report/figures/
```

`plots.py` keeps the per-run numbers it plots in `docs/report/figures/data/`.

## Layout

```
scheduler/   scx_kairos.bpf.c (policy), scx_kairos.c (loader), scx_kairos.h, Makefile
workloads/   grpcserver/ and loadgen/ (Go), batch/ (zstd), traces/ (phase generators)
experiments/ run.sh (harness), configs/, scripts that run several configs in a row
analysis/    per-run summaries, comparisons, statistical tests
ml/          the LC-demand predictor (an extension; no gain measured)
scripts/     helpers (run metadata, sampling, cache eviction, log entries)
docs/        PROJECT_PLAN.md (plan and status), RESEARCH_LOG.md (decisions, every experiment, results)
```

## License

The scheduler is GPL-2.0, like the sched_ext examples it is based on
(scx_simple, Copyright (c) 2022 Meta Platforms, Inc. and affiliates).
