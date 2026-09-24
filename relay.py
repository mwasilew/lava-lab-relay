# Forward TCP ports to services on the LAVA dispatcher's Docker network, so that a
# LAVA docker test container (which runs on the default bridge and cannot resolve
# those services) can reach them through ports published on the worker.
#
#   FORWARDS="7085=ser2net:7085,7080=tac-api:80"
#
# Each entry is LISTEN_PORT=HOST:PORT. Connections are relayed byte-for-byte in both
# directions; nothing is interpreted, so telnet (ser2net) and HTTP (tac-api) both work.
import os
import socket
import sys
import threading


def pump(src, dst):
    try:
        while True:
            data = src.recv(4096)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        for s in (src, dst):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def forward(listen, upstream):
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", listen))
    srv.listen(8)
    print(f"forwarding :{listen} -> {upstream[0]}:{upstream[1]}", flush=True)
    while True:
        client, addr = srv.accept()
        try:
            up = socket.create_connection(upstream, timeout=10)
            up.settimeout(None)
        except OSError as exc:
            print(f"{addr} -> {upstream}: upstream failed: {exc}", flush=True)
            client.close()
            continue
        threading.Thread(target=pump, args=(client, up), daemon=True).start()
        threading.Thread(target=pump, args=(up, client), daemon=True).start()


def parse(spec):
    forwards = []
    for entry in filter(None, (e.strip() for e in spec.split(","))):
        listen, target = entry.split("=", 1)
        host, port = target.rsplit(":", 1)
        forwards.append((int(listen), (host, int(port))))
    return forwards


def main():
    try:
        forwards = parse(os.environ.get("FORWARDS", "7085=ser2net:7085"))
    except ValueError as exc:
        sys.exit(f"bad FORWARDS: {exc}")
    threads = [threading.Thread(target=forward, args=f, daemon=True) for f in forwards]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


if __name__ == "__main__":
    main()
