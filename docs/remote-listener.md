# Remote hrpyc Listener Activation

The Houdini MCP plugin can start a remotely reachable **hrpyc** listener so an
external MCP server (for example the Docker-based gateway) can connect to a
running Houdini session over RPyC.

This replaces the friction of manually running `import hrpyc;
hrpyc.start_server()` in the Python shell and then discovering the listener is
unreachable because of bind/firewall ambiguity.

> The **manual/direct** workflow still works exactly as before. The activation
> path described here is an additional, safer entry point — it does not change
> or remove the manual mode.

## Quick start (from the shelf)

The **Houdini MCP** shelf provides:

| Tool               | Action                                                            |
| ------------------ | ---------------------------------------------------------------- |
| **Start Remote**   | Start the hrpyc listener (bind/security from environment)        |
| **Stop Remote**    | Stop the listener (idempotent)                                   |
| **Remote Status**  | Show bind host, actual bound port, exposure, auth                |
| **Remote Self-Test** | Reachability + versions + loopback warning + firewall guidance |

## Configuration (environment variables)

| Variable                      | Default     | Meaning                                             |
| ----------------------------- | ----------- | --------------------------------------------------- |
| `HOUDINI_RPC_BIND_HOST`       | `127.0.0.1` | Bind address. Loopback is local-only and safe.      |
| `HOUDINI_RPC_BIND_PORT`       | `18811`     | TCP port (`0` = OS-assigned).                       |
| `HOUDINI_RPC_TRUSTED_NETWORK` | `0`         | Set `1` to allow a non-loopback bind (opt-in).      |
| `HOUDINI_RPC_TOKEN`           | *(unset)*   | Shared secret; enables authenticated remote bind.   |

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
  (`houdini_plugin/python/houdini_mcp_handshake`), so the package JSON puts it
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
self-test also emits per-OS firewall commands for the bound port.

## Live verification (opt-in, read-only)

`scripts/verify_remote_listener.py` connects to an already-running listener and
reads the Houdini version. It is **read-only** and never starts/stops/restarts
Houdini. It only runs when explicitly enabled:

```bash
RUN_LIVE_REMOTE_CHECK=1 \
HOUDINI_HOST=127.0.0.1 HOUDINI_PORT=18811 \
python scripts/verify_remote_listener.py
```

Exit codes: `0` reachable/responsive, `1` failed, `2` disabled/misconfigured.
