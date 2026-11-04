# YAAH

![YAAH box art - the AI coding agent for everyone - Halloween edition](docs/assets/box-art-halloween.png)

**YAAH** = **Y**et **A**nother **A**gent Harness.

YAAH is a desktop AI coding agent: a model with real tools on your machine —
shell, files, git, search — and every tool call shown transparently. See
`PRODUCT.md` for the product framing.

## Download

Prebuilt installers (Windows / Linux) are on the
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

## Headless server (`yaah-server-setup`)

The same host role, without the GUI — as a **background service** that
survives reboots. One self-contained artifact per OS: `yaah-server-setup.exe`
on Windows (double-click → wizard installs the auto-start service, no
companion files) and `yaah-server-setup.deb` on Linux (systemd daemon,
passphrase via `/etc/yaah/yaah.conf`). See
[SERVER-SETUP.md](SERVER-SETUP.md) for the full installation guide.

The server build is deliberately lean: no voice (whisper/sherpa-onnx)
dependencies, because a remote client only forwards workspace tools to its
host — and #339 removed the host computer-use stack entirely, for every
host. GUI control lives inside the Windows Sandbox via windows-mcp.

## Bundled tools and licenses

Desktop and headless-server builds include the official [GitHub CLI](https://cli.github.com/)
(`gh`) so agent shell commands can use it without a separate installation. The
CLI is MIT-licensed; its copyright and permission notice is shipped alongside
the binary. GitHub CLI also embeds platform-specific third-party license
information, available with `gh licenses`. Bundling the CLI does not configure
a GitHub account: authenticate with `gh auth login` or the usual `GH_TOKEN`
environment variable.

GitHub CLI is an independent project; YAAH is not affiliated with or endorsed
by GitHub. See the [GitHub CLI license](https://github.com/cli/cli/blob/trunk/LICENSE)
and [license-compliance notes](https://github.com/cli/cli/blob/trunk/docs/license-compliance.md).
