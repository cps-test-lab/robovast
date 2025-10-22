#!/usr/bin/env python3
import argparse
import csv
import json
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlencode


def do_request(base_url: str, timeout_s: float, params: dict, req_id: int):
    url = f"{base_url}?{urlencode(params)}" if params else base_url
    t0 = time.perf_counter()
    err = None
    code = None
    ok = False
    body_latency = None
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as resp:
            code = resp.getcode()
            raw = resp.read()
            # Server liefert selbst eine latency_ms; wir messen zusätzlich end-to-end
            try:
                body = json.loads(raw.decode("utf-8"))
                body_latency = body.get("latency_ms")
                ok = bool(body.get("ok", False)) and code == 200
            except Exception:
                ok = (code == 200)
    except urllib.error.HTTPError as e:
        code = e.code
        err = f"HTTPError {e.code}"
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
    end_to_end_ms = (time.perf_counter() - t0) * 1000.0
    return {
        "id": req_id,
        "http_code": code,
        "ok": ok,
        "error": err,
        "e2e_ms": round(end_to_end_ms, 2),
        "server_latency_ms": body_latency
    }


def main():
    ap = argparse.ArgumentParser(description="Single test run client (CSV output).")
    ap.add_argument("--url", default="http://localhost:8000/call")
    # serverseitige sim-parameter
    ap.add_argument("--server-delay-ms", type=int, default=250)
    ap.add_argument("--server-jitter-ms", type=int, default=100)
    ap.add_argument("--server-fail", type=float, default=0.2)
    ap.add_argument("--server-payload-kb", type=int, default=0)
    # client-parameter
    ap.add_argument("--timeout-ms", type=int, default=500)
    ap.add_argument("--requests", type=int, default=100)
    ap.add_argument("--concurrency", type=int, default=1)
    ap.add_argument("--output", default="-", help="CSV-file or '-' für stdout")
    args = ap.parse_args()

    params = {
        "server_delay_ms": args.server_delay_ms,
        "server_jitter_ms": args.server_jitter_ms,
        "server_fail": args.server_fail,
        "server_payload_kb": args.server_payload_kb
    }

    # Work items
    jobs = list(range(args.requests))
    results = []

    t_batch0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as ex:
        futs = [ex.submit(do_request, args.url, args.timeout_ms/1000.0, params, i) for i in jobs]
        for f in as_completed(futs):
            results.append(f.result())
    total_ms = (time.perf_counter() - t_batch0) * 1000.0

    # CSV schreiben
    out = sys.stdout if args.output == "-" else open(args.output, "w", newline="")
    writer = csv.writer(out)
    writer.writerow(["req_id", "ok", "http_code", "error", "e2e_ms", "server_latency_ms",
                     "server_delay_ms", "server_jitter_ms", "server_fail", "server_payload_kb",
                     "timeout_ms", "concurrency", "requests"])
    for r in sorted(results, key=lambda x: x["id"]):
        writer.writerow([
            r["id"], int(r["ok"]), r["http_code"] if r["http_code"] is not None else "",
            r["error"] or "", r["e2e_ms"], r["server_latency_ms"] if r["server_latency_ms"] is not None else "",
            args.server_delay_ms, args.server_jitter_ms, args.server_fail, args.server_payload_kb,
            args.timeout_ms, args.concurrency, args.requests
        ])
    if out is not sys.stdout:
        out.close()

    # kompakte Zusammenfassung auf STDERR
    ok_count = sum(1 for r in results if r["ok"])
    err_count = len(results) - ok_count
    e2e_vals = [r["e2e_ms"] for r in results if r["e2e_ms"] is not None]
    p95 = sorted(e2e_vals)[int(0.95*len(e2e_vals))-1] if e2e_vals else None
    throughput = (ok_count / (total_ms/1000.0)) if total_ms > 0 else 0.0

    sys.stderr.write(
        f"\n--- SUMMARY ---\n"
        f"success: {ok_count}/{len(results)}  "
        f"errors: {err_count}  "
        f"mean e2e (ms): {sum(e2e_vals)/len(e2e_vals):.1f}  "
        f"p95 e2e (ms): {p95:.1f}  "
        f"throughput (ok/s): {throughput:.2f}  "
        f"total time (ms): {total_ms:.1f}\n"
    )


if __name__ == "__main__":
    main()
