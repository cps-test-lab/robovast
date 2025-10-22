#!/usr/bin/env python3
import json
import random
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse


class Handler(BaseHTTPRequestHandler):
    server_version = "MiniBTService/0.1"

    def log_message(self, fmt, *args):
        # comment out to silence console output
        super().log_message(fmt, *args)
        # pass

    def _send_json(self, status, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        p = urlparse(self.path)
        if p.path == "/health":
            return self._send_json(200, {"ok": True})

        if p.path != "/call":
            return self._send_json(404, {"ok": False, "error": "unknown endpoint"})

        q = parse_qs(p.query)
        # parameters with defaults
        delay_ms = float(q.get("delay_ms",  ["250"])[0])   # base latency
        jitter_ms = float(q.get("jitter_ms", ["0"])[0])     # extra random jitter [0..jitter_ms]
        fail_p = float(q.get("fail",     ["0.1"])[0])    # failure probability 0..1

        # simulate latency
        sleep_ms = max(0.0, delay_ms + (random.uniform(0, jitter_ms) if jitter_ms > 0 else 0.0))
        t0 = time.perf_counter()
        time.sleep(sleep_ms / 1000.0)
        latency_ms = (time.perf_counter() - t0) * 1000.0

        # success or failure?
        ok = random.random() >= fail_p
        payload = {
            "ok": ok,
            "latency_ms": round(latency_ms, 2),
            "params": {"delay_ms": delay_ms, "jitter_ms": jitter_ms, "fail": fail_p}
        }
        self._send_json(200 if ok else 503, payload)


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Minimal test service for retry/deadline experiments.")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()
    print(f"🚀 Mini service running at http://{args.host}:{args.port}  (CTRL+C to stop)")
    print(f"   Endpoints: /health, /call?delay_ms=250&jitter_ms=100&fail=0.2")
    with HTTPServer((args.host, args.port), Handler) as srv:
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            print("\nStopping…")


if __name__ == "__main__":
    main()
