# Remote hrpyc Listener Activation

The Houdini MCP plugin can start a remotely reachable **hrpyc** listener so an
external MCP server (for example the Docker-based gateway) can connect to a
running Houdini session over RPyC.

This replaces the friction of manually running `import hrpyc;
hrpyc.start_server()` in the Python shell and then discovering the listener is
unreachable because of bind/firewall ambiguity.

> The **manual/direct** workflow still serves every tool, though `find_nodes`,
> `get_parameter_schema` and the tools that edit the scene as one undo step need the plugin installed
> either way. The activation path described here is an additional, safer entry
> point — it does not change or remove the manual mode.

## Quick start (from the shelf)

The **Houdini MCP** shelf provides:

| Tool               | Action                                                            |
| ------------------ | ---------------------------------------------------------------- |
| **Start Remote**   | Start the hrpyc listener (bind/security from environment)        |
| **Stop Remote**    | Stop the listener (idempotent)                                   |
| **Remote Status**  | Show bind host, actual bound port, exposure, auth; or that the listener stopped serving |
| **Remote Self-Test** | Reachability + versions + loopback warning + firewall guidance |

A listener whose server stopped serving under it (the accept loop ended, the
socket closed) is **stale**: `get_hrpyc_status()` and `self_test_hrpyc()`
report `running: False, stale: True` with the endpoint, `is_hrpyc_running()`
is `False`, and **Start Remote** starts a fresh listener in its place.

**Stop Remote** with clients connected disconnects them: each one's next call
fails with a closed connection, and Houdini's log records
`hrpyc client ('<host>', <port>) disconnected from port <port> by the listener (<how>)`
for each, not an error. A client that closes its own connection is recorded
`by the client`. `<how>` is how the thread serving the client saw the end:
the exception it ended on, such as `EOFError: connection closed by peer` or
`OSError: [Errno 9] Bad file descriptor`, or `between rounds` when a
one-second round ended with no exception and found serving over. A stop
recorded `between rounds` is usually one whose close never woke the thread.

## Configuration (environment variables)

| Variable                      | Default     | Meaning                                             |
| ----------------------------- | ----------- | --------------------------------------------------- |
| `HOUDINI_RPC_BIND_HOST`       | `127.0.0.1` | Bind address. Loopback is local-only and safe.      |
| `HOUDINI_RPC_BIND_PORT`       | `18811`     | TCP port (`0` = OS-assigned).                       |
| `HOUDINI_RPC_TRUSTED_NETWORK` | `0`         | Set `1` to allow a non-loopback bind (opt-in).      |
| `HOUDINI_RPC_TOKEN`           | *(unset)*   | Shared secret; enables authenticated remote bind.   |

A variable set to something it cannot mean (a port that is not a decimal
integer from 0 to 65535, a flag that is not `1`/`true`/`yes`/`on` or
`0`/`false`/`no`/`off`)
refuses to start the listener with a `ValueError` naming the variable and the
value. Nothing falls back to a default: an unset or empty variable is the only
way to get one. The **Start Remote** port prompt is held to the same rule.

## Security model — no silent exposure

`hrpyc`/RPyC `SlaveService` grants **arbitrary remote code execution** inside
Houdini. To prevent accidentally exposing that to the network, the listener
enforces this policy:

- **A configured token is enforced or nothing binds**, whatever the host (see
  the hardening notes below).
- **Loopback binds** (`127.0.0.1`, `::1`, `localhost`) are otherwise always
  allowed and are reachable only from the same machine.
- **Any non-loopback bind** (`0.0.0.0` or a specific LAN IP) is **refused**
  unless the operator explicitly opts in via **either**:
  - `HOUDINI_RPC_TRUSTED_NETWORK=1` (you attest the network is trusted), or
  - `HOUDINI_RPC_TOKEN=<secret>` (clients must present the token first).

If neither is set, `Start Remote` returns a security error and **no listener is
created** — there is no code path that silently binds `0.0.0.0` without auth.
A refused config changes nothing: `reload_hrpyc_server()` checks the new config
before it stops anything, so the running listener keeps serving, and the error
says where. A config that is admitted but fails to start (its port is taken,
say) leaves the listener that was serving before serving again on its old
config; the error says why the reload failed and where the listener serves, or,
if the old config cannot start again either, that nothing is serving and why.
Its clients are disconnected by the stop either way.

### Limitations (read this)

- The token authenticator is a lightweight shared-secret handshake performed
  before the RPyC protocol. It is **not** TLS and provides **no encryption** —
  use it only on trusted networks or behind an encrypted tunnel
  (WireGuard, SSH, Cloudflare Tunnel, etc.).
- The handshake has one definition, `houdini_mcp_handshake`: the listener
  enforces its server side and the MCP server (`HOUDINI_RPC_TOKEN` in the
  server's environment) and `scripts/verify_remote_listener.py` present its
  client side. Set the same `HOUDINI_RPC_TOKEN` on both machines. The token is
  sent length-prefixed and the listener acknowledges a correct one with one
  byte, so a client presenting a wrong token is told so at connect time and
  does not retry, while a listener that never answers (it enforces no token,
  or Houdini is too busy) is a timeout, retried like any other. A client
  presenting no token to a tokened listener sees a connection failure that
  names the token as a cause. The package lives in the plugin tree
  (`houdini_plugin/houdini_mcp/python/houdini_mcp_handshake`), so the package JSON puts it
  on Houdini's path, and the server distribution ships the same source. The
  listener imports it only when a token is configured; if Houdini's `rpyc`
  lacks the authenticator primitives, the bind is refused with a message
  saying so. The listener logs every accepted and rejected handshake under
  the `houdini_mcp_handshake` logger.
- A configured token is always enforced: if the installed `rpyc` cannot provide
  the authenticator primitives, the security policy refuses the bind (a
  `security` error from `start()`, or `SecurityError` with `raise_on_policy`)
  and nothing is bound, rather than running unauthenticated. The reported
  `authenticated` value reflects the authenticator actually wired into the
  server.
- `SlaveService` is inherently powerful; treat any reachable listener as a
  full remote shell into Houdini.

## Loopback-only warning & firewall guidance

If the listener is bound to loopback, `Remote Status` / `Remote Self-Test`
warn that **remote clients cannot reach it** and explain how to re-bind. The
self-test also emits the firewall commands for the OS Houdini runs on. On Linux
and Windows they open the bound port. The macOS Application Firewall admits programs, not ports, so the
macOS command allows the executable that owns the listener's socket, which the
guidance names; `self_test_hrpyc()` on a running listener also returns it as
`executable`. In graphical Houdini, that is the binary the
launcher started: `bin/houdinifx` starts `Contents/MacOS/houdinifx`, and
`bin/houdini` starts `Contents/MacOS/houdini`. In `hython`, it is `hython`.

## Live verification (opt-in, read-only)

`scripts/verify_remote_listener.py` connects to an already-running listener and
reads the Houdini version. It is **read-only** and never starts/stops/restarts
Houdini. It only runs when explicitly enabled:

```bash
RUN_LIVE_REMOTE_CHECK=1 \
HOUDINI_HOST=127.0.0.1 HOUDINI_PORT=18811 \
uv run python scripts/verify_remote_listener.py
```

Exit codes: `0` reachable/responsive, `1` failed, `2` disabled/misconfigured.
