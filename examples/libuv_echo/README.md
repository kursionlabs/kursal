# libuv echo

A one-client TCP echo server over [libuv](https://libuv.org/). It shows how a Kursal package wraps a
C library: the application is safe code, and a small C shim owns libuv's callbacks and buffers. Its
interface is experimental, not a general networking package or an async framework.

## What it shows

[`echo.kur`](echo.kur) is the application. It chooses to echo each read:

```kursal
fn echo(input: Buffer!) -> TcpAction! { TcpAction!::Write.(input) }
```

It calls `TcpServer!::bind_local(0u16, echo)`, prints the assigned port, then calls `server.run()`,
with no raw pointers or `unsafe` calls. The handler can also return `TcpAction!::Continue` or
`TcpAction!::Close` after consuming the input. It is a plain function pointer because the read
callback can run more than once.

[`tcp.kur`](tcp.kur) owns the Kursal side of the boundary with C. For each read, it copies the
borrowed C bytes into an owned `Buffer!`, invokes the handler, and carries out its action. For
`Write`, it copies the returned buffer into C-owned write storage, then destroys the Kursal buffer.
If the write request fails to start, both the C request and its copy are freed at once; otherwise
the C write callback frees them on completion. On EOF, the C shim stops reading, drains pending
writes, calls `uv_close`, and frees the client's storage in its close callback. `TcpServer!::run`
consumes the server and frees its callback payload after the loop has drained; `stop` consumes a
bound server before `run` if printing the port fails.

[`kur.toml`](kur.toml) adds the shim, [`native/tcp_shim.c`](native/tcp_shim.c), with
`c_sources = ["native/tcp_shim.c"]`, and links libuv with `link_flags = ["-luv"]`.

## Building and running

Building needs libuv's headers and library. On Debian or Ubuntu:

```sh
sudo apt install libuv1-dev
```

From the repository root, after `scripts/bootstrap.sh`:

```sh
bin/kursal build examples/libuv_echo -o /tmp/libuv_echo
python3 examples/libuv_echo/test_loopback.py /tmp/libuv_echo
```

The server binds to `127.0.0.1` on an ephemeral port, prints that port, serves one client, and
exits once its pending writes complete. [`test_loopback.py`](test_loopback.py) starts it twice: one
client connects and sends EOF without data; the other sends 65,549 bytes and checks the exact echo
before EOF.

## Limits

The server serves one client, and offers no cancellation, write backpressure or stateful handler.
It reports libuv error codes without their descriptions. The C shim aborts if libuv reports a
drained loop as still busy. The shim's assumptions about callback order and storage lifetimes have
not had a full review of the unsafe boundary, so this is not a safe general API.
