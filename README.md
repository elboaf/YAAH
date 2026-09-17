# YAAH

YAAH is a desktop AI coding agent: a model with real tools on your machine —
shell, files, git, search — and every tool call shown transparently. See
`PRODUCT.md` for the product framing.

## Download

Prebuilt installers (Windows / macOS / Linux) are on the
[Releases](../../releases) page. Apps are unsigned — expect an OS warning on
first run.

## Remote hosting

Any YAAH instance can act as a **host**: it advertises itself on the LAN
(mDNS) and executes workspace tools (shell, files, git) for a **client** —
another YAAH desktop app. Conversations, provider keys and the agent loop
always stay on the client; the host is a stateless executor. Enable it under
Settings → hosting: set a passphrase (required — a host without one refuses
all remote exec), then connect from another machine via the host switcher or
a direct `IP:8765` entry.

## Headless server (`yaah-server`)

The same host role, without the GUI: a standalone `yaah-server` executable
(attached to every release as `yaah-server-<os>`) that runs from a plain
shell — no display, no Python, no installer. Run it on the target device:

```
yaah-server --passphrase <secret>
```

Then connect from the YAAH desktop app: the server appears in the host
switcher automatically, or enter the device's IP directly (default port
8765), using the same passphrase.

| Option | Meaning |
| --- | --- |
| `--host H` | bind address (default `0.0.0.0`) |
| `--port P` | listen port (default `8765`) |
| `--passphrase SECRET` | save the passphrase clients must present |
| `--display-name NAME` | name shown in the desktop app's host switcher |
| `--no-hosting` | skip LAN discovery; connect by direct IP only |

`--passphrase` and `--display-name` persist to `~/.yaah/config.json` — the
same store the desktop app's Settings writes — so they survive restarts and
both apps see the same values. Without a passphrase the server still starts
and is discoverable, but every remote exec request is refused (401).

The server build is deliberately lean: no voice (whisper/sherpa-onnx) and no
computer-use dependencies, because a remote client only forwards workspace
tools to its host.

## Building from source

```
npm ci
bash scripts/build_sidecar.sh   # desktop backend (PyInstaller)
npx tauri build                 # desktop installers
bash scripts/build_server.sh    # headless server -> dist/yaah-server
```

## Tests

```
python -m pytest
```
