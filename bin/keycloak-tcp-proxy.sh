#!/usr/bin/env bash
# polaris POC — tiny TCP proxy so the screenshot script can resolve
# `keycloak` to 127.0.0.1:8080 from the browser.
#
# The portals run inside the docker-compose network and reference Keycloak
# via `http://keycloak:8080/...`. From the host, Keycloak is reachable
# only on :8081 (docker-compose publish mapping). For Playwright's
# chromium browser to follow the OIDC redirect, we need Keycloak on
# :8080. This proxy listens on :8080 and forwards to :8081, rewriting
# the Host header so Keycloak serves the right realm.
#
# Usage: bin/keycloak-tcp-proxy.sh [start|stop|status]
set -euo pipefail

PID_FILE="${TMPDIR:-/tmp}/keycloak-tcp-proxy.pid"
LOG_FILE="${TMPDIR:-/tmp}/keycloak-tcp-proxy.log"

start() {
  if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "already running (PID $(cat "$PID_FILE"))"
    return 0
  fi
  python3 - <<'PY' >>"$LOG_FILE" 2>&1 &
import socket, threading
LISTEN_HOST, LISTEN_PORT = "127.0.0.1", 8080
TARGET_HOST, TARGET_PORT = "127.0.0.1", 8081
TARGET_HOSTHEADER = "localhost:8081"

def pipe(src, dst, rewrite_host=None):
    try:
        buf = b""
        while True:
            chunk = src.recv(4096)
            if not chunk:
                break
            if rewrite_host and b"\r\nHost:" in buf + chunk:
                # Reassemble the head buffer to find the Host line.
                full = (buf + chunk)
                head, _, body = full.partition(b"\r\n\r\n")
                lines = head.split(b"\r\n")
                new_lines = []
                for line in lines:
                    if line.lower().startswith(b"host:"):
                        new_lines.append(b"Host: " + TARGET_HOSTHEADER.encode())
                    else:
                        new_lines.append(line)
                chunk = b"\r\n".join(new_lines) + b"\r\n\r\n" + body
                buf = b""
                dst.sendall(chunk)
                chunk = b""
            else:
                buf += chunk
                # Don't accumulate too much before flushing
                if len(buf) > 65536:
                    dst.sendall(buf)
                    buf = b""
        if buf:
            dst.sendall(buf)
    except Exception:
        pass
    finally:
        for s in (src, dst):
            try: s.close()
            except Exception: pass

srv = socket.socket()
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind((LISTEN_HOST, LISTEN_PORT))
srv.listen(50)
print(f"proxy listening on {LISTEN_HOST}:{LISTEN_PORT} -> {TARGET_HOST}:{TARGET_PORT} (Host rewrite → {TARGET_HOSTHEADER})", flush=True)
while True:
    c, _ = srv.accept()
    t = socket.socket()
    t.connect((TARGET_HOST, TARGET_PORT))
    threading.Thread(target=pipe, args=(c, t, "client_to_proxy"), daemon=True).start()
    threading.Thread(target=pipe, args=(t, c, None), daemon=True).start()
PY
  PID=$!
  echo $PID > "$PID_FILE"
  sleep 1
  if ! kill -0 "$PID" 2>/dev/null; then
    echo "FAILED to start (see $LOG_FILE)" >&2
    return 1
  fi
  echo "started (PID $PID, log $LOG_FILE)"
}

stop() {
  if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    if kill -0 "$PID" 2>/dev/null; then
      kill "$PID"
      echo "stopped (PID $PID)"
    fi
    rm -f "$PID_FILE"
  else
    echo "not running"
  fi
}

status() {
  if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "running (PID $(cat "$PID_FILE"))"
  else
    echo "not running"
  fi
}

case "${1:-status}" in
  start)  start ;;
  stop)   stop ;;
  status) status ;;
  *) echo "usage: $0 [start|stop|status]" >&2; exit 2 ;;
esac