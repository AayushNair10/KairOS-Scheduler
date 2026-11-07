#!/usr/bin/env python3
"""Run checks for one results directory, and the service's CPU per request.

Per run, in the order the runs ran:
- F's run checks (RESEARCH_LOG 5.1): VM time share, other processes' CPU
  (% of one CPU, over the load), pages swapped in, direct-reclaim stalls.
  A run is flagged ("FLAG") if VM share < 0.95, other > 30%, swap-ins
  > 1,000 or reclaim stalls > 1,000; stats.py --skip-runs leaves out its
  pairs.
- The service's (grpcserver or redis-server) CPU per completed request,
  user and system, and context switches (all CPUs) per completed request.
  CPU and context switches are counted between run.sh's snapshots, which
  bracket the load generator's whole run: the load, then waiting for every
  reply. Every request is either answered or failed (timeout) by then, so
  the completed requests (requests - errors) all fall inside the interval.
  The snapshots also take in the load generator's start and exit, when
  the service is idle; "extra" is the interval minus the load's duration.
  The checks above don't see a host that runs the VM's CPUs slower
  (section 8, 2026-11-06); the service's CPU per request does.

Then per config and per rep: the median service CPU per request, to see
differences between configs and drift over a session.

Usage: analysis/checks.py results/<date>-<name> [...]
"""
import collections
import glob
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import runlib  # noqa: E402

SERVICES = ("grpcserver", "redis-server")


def run_row(d):
    st, mem = runlib.system_stats(d), runlib.memory_stats(d) or {}
    ph = runlib.load_summary(d)["phases"]
    load_s = (ph[-1]["end_unix_ns"] - ph[0]["start_unix_ns"]) / 1e9
    other = 100 * sum(t for _, t in runlib.other_cpu(d) or []) / load_s
    swap, stalls = mem.get("swap_in", 0), mem.get("reclaim_stalls", 0)
    flags = [f for f, bad in (("vm", st["vm_share"] < 0.95), ("other", other > 30),
                              ("swap", swap > 1000), ("reclaim", stalls > 1000)) if bad]
    done = sum(p["requests"] - p["errors"] for p in ph)
    user = sys_ = 0.0
    for c in SERVICES:
        u, s = runlib.proc_times(d, c)
        user, sys_ = user + u, sys_ + s
    snap = (os.stat(os.path.join(d, "procs_after")).st_mtime_ns
            - os.stat(os.path.join(d, "procs_before")).st_mtime_ns) / 1e9
    cfg, rep = os.path.basename(d).rsplit("-r", 1)
    return {"cfg": cfg, "rep": int(rep), "start": ph[0]["start_unix_ns"],
            "vm": st["vm_share"], "other": other, "swap": swap, "stalls": stalls,
            "flags": flags, "done": done, "errors": sum(p["errors"] for p in ph),
            "user_us": 1e6 * user / done, "sys_us": 1e6 * sys_ / done,
            "ctxt": st["ctxt_per_s"] * st["elapsed_s"] / done, "extra_s": snap - load_s}


def main():
    for top in sys.argv[1:]:
        rows = sorted((run_row(d) for d in glob.glob(os.path.join(top, "*-r*"))
                       if os.path.exists(os.path.join(d, "procs_after"))),
                      key=lambda r: r["start"])
        print(f"{top}: {len(rows)} runs, {sum(bool(r['flags']) for r in rows)} flagged")
        print("  run                              start    vm other%  swapin reclaim"
              "   done errors srv us/req (user+sys) ctxsw/req extra s")
        for r in rows:
            name = f"{r['cfg']}-r{r['rep']}"
            print(f"  {name:32s} {time.strftime('%H:%M', time.localtime(r['start'] / 1e9))}"
                  f" {r['vm']:5.2f} {r['other']:6.1f} {r['swap']:7d} {r['stalls']:7d}"
                  f" {r['done']:6d} {r['errors']:6d}"
                  f" {r['user_us'] + r['sys_us']:7.1f} ({r['user_us']:5.1f}+{r['sys_us']:5.1f})"
                  f" {r['ctxt']:9.2f} {r['extra_s']:7.1f}"
                  + ("  FLAG " + ",".join(r["flags"]) if r["flags"] else ""))
        for key, label in (("cfg", "config"), ("rep", "rep")):
            groups = collections.defaultdict(list)
            for r in rows:
                groups[r[key]].append(r)
            print(f"  median per {label}: srv us/req, of which user and sys (each its own"
                  " median), ctxsw/req")
            for k, rs in sorted(groups.items()):
                med = {f: statistics.median(r[f] for r in rs) for f in ("user_us", "sys_us", "ctxt")}
                tot = statistics.median(r["user_us"] + r["sys_us"] for r in rs)
                print(f"    {str(k):30s} {tot:7.1f} user {med['user_us']:5.1f} sys {med['sys_us']:5.1f}"
                      f" {med['ctxt']:9.2f}")


if __name__ == "__main__":
    main()
