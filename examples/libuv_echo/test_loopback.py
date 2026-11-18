"""Manual loopback test for the optional libuv echo example."""

import socket
import subprocess
import sys
from pathlib import Path

BINARY = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp/libuv_echo")


def exchange(payload: bytes) -> None:
    server = subprocess.Popen([str(BINARY)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert server.stdout is not None and server.stderr is not None
        port = int(server.stdout.readline())
        with socket.create_connection(("127.0.0.1", port), timeout=3) as client:
            client.settimeout(5)
            client.sendall(payload)
            client.shutdown(socket.SHUT_WR)
            received = bytearray()
            while chunk := client.recv(8192):
                received.extend(chunk)
        status = server.wait(timeout=5)
        assert (bytes(received), status) == (payload, 0), (len(received), status, server.stderr.read())
        print(f"echoed {len(payload)} bytes; exit {status}")
    finally:
        if server.poll() is None:
            server.kill()
            server.wait()


exchange(b"")
exchange(b"hello, libuv\n" + b"x" * 65536)
