#!/usr/bin/env python3
import json
import random
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse


class Handler(BaseHTTPRequestHandler):
    server_version = "MiniBTService/0.1"
    # class variables to store server configuration
    payload_kb = 0
    delay_ms = 250.0
    jitter_ms = 0.0
    fail_p = 0.1

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

        # Use class variables for server configuration
        delay_ms = self.delay_ms
        jitter_ms = self.jitter_ms
        fail_p = self.fail_p

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
        
        # add padding if payload_kb is set
        if self.payload_kb > 0:
            # Calculate how much padding we need
            # Each character is 1 byte in UTF-8 for ASCII
            current_size = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
            target_size = self.payload_kb * 1024
            padding_size = max(0, target_size - current_size)
            if padding_size > 0:
                payload["padding"] = "x" * padding_size
        
        self._send_json(200 if ok else 503, payload)


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Minimal test service for retry/deadline experiments.")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--delay-ms", type=float, required=True, help="Base latency in milliseconds")
    ap.add_argument("--jitter-ms", type=float, required=True, help="Random jitter in milliseconds")
    ap.add_argument("--fail", type=float, required=True, help="Failure probability 0..1")
    ap.add_argument("--payload-kb", type=int, required=True, help="Response payload size in kilobytes")
    args = ap.parse_args()
    
    # Set configuration on the Handler class
    Handler.delay_ms = args.delay_ms
    Handler.jitter_ms = args.jitter_ms
    Handler.fail_p = args.fail
    Handler.payload_kb = args.payload_kb
    
    print(f"🚀 Mini service running at http://{args.host}:{args.port}  (CTRL+C to stop)")
    print(f"   Endpoints: /health, /call")
    print(f"   Configuration: delay={args.delay_ms}ms, jitter={args.jitter_ms}ms, fail={args.fail}, payload={args.payload_kb}KB")
    with HTTPServer((args.host, args.port), Handler) as srv:
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            print("\nStopping…")


if __name__ == "__main__":
    main()
