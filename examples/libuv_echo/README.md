# libuv echo

This one-client TCP echo server shows what a small libuv integration looks like
in Kursal. Building it needs libuv headers and a linkable `-luv` on the host.
Its interface is **experimental**, not a general networking package or an async framework.

From the repository root:

```sh
pkg-config --modversion libuv
bin/kursal build examples/libuv_echo -o /tmp/libuv_echo
python examples/libuv_echo/test_loopback.py /tmp/libuv_echo
```

The test starts two server processes. One client connects and sends EOF without
data; the other sends 65,549 bytes and checks the exact echo before EOF. The
server binds to `127.0.0.1` on an ephemeral port, prints that port, serves one
client, and exits after its pending writes complete.

[`echo.kur`](echo.kur) is the application. It chooses to echo each read:

```kursal
fn echo(input: Buffer!) -> TcpAction! { TcpAction!::Write.(input) }
```

It calls `TcpServer!::bind_local(0u16, echo)`, prints the assigned port, then
calls `server.run()`. There are no raw pointers or `unsafe` calls in application code.
The handler can also return `TcpAction!::Continue` or `TcpAction!::Close` after
consuming the input. It is a plain function pointer because the read callback
can run more than once.

[`tcp.kur`](tcp.kur) owns the Kursal side of the FFI boundary.
For each read, it copies the borrowed C bytes into an owned `Buffer!`, invokes
the handler, and carries out its action. For `Write`, it copies the returned
buffer into C-owned write storage, then destroys the Kursal buffer. If the
write request fails to start, both the C request and its copy are freed
immediately. Otherwise the C write callback
frees them on completion. On EOF, the C shim stops reading, drains pending
writes, calls `uv_close`, and frees client storage in its close callback.
`TcpServer!::run` consumes the server and frees its callback payload after
the loop has drained. `stop` consumes a bound server before `run` if printing
the port fails.

[`kur.toml`](kur.toml) uses package-relative `c_sources` for
[`native/tcp_shim.c`](native/tcp_shim.c) and `link_flags = ["-luv"]`.

The self-hosted compiler built this package, and the loopback test passed for
both an empty stream and 65,549 bytes. The same exchanges passed under
AddressSanitizer with leak detection disabled. LeakSanitizer could not run in
the development sandbox because its ptrace restrictions block it.

The public API still has intentional limits: it serves one client, offers no
cancellation, write backpressure, or stateful handler, and reports libuv error
codes without descriptions. The C shim aborts if libuv says a supposedly
drained loop is still busy. Its callback and storage lifetime assumptions need
a full unsafe-boundary review before this could be called a safe general API.
