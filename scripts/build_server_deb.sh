#!/usr/bin/env bash
# Package the Linux yaah-server PyInstaller binary (dist/yaah-server, built
# by scripts/build_server.sh) as a .deb that installs a systemd daemon:
#
#   /usr/bin/yaah-server                       the server (standalone exe)
#   /usr/lib/systemd/system/yaah-server@.service  template unit, per-user
#   /etc/yaah/yaah.conf.example                passphrase config template
#
# postinst enables + starts yaah-server@<primary-login-user> (first real
# user with a home dir and a login shell), so a plain `apt install ./…deb`
# yields a running, auto-start-at-boot daemon whose workspace is that
# user's home. The passphrase comes from /etc/yaah/yaah.conf (loaded by the
# unit's EnvironmentFile as YAAH_PASSPHRASE, read by backend/main.py);
# without it the daemon runs but refuses all remote requests.
#
# Output: dist/yaah-server_<version>_all.deb
set -euo pipefail
cd "$(dirname "$0")/.."

[ -f dist/yaah-server ] || { echo "error: dist/yaah-server missing (run build_server.sh on Linux first)"; exit 1; }

VERSION=$(node -p "require('./package.json').version")
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT

install -Dm755 dist/yaah-server "$STAGE/usr/bin/yaah-server"
install -Dm644 /dev/null "$STAGE/etc/yaah/yaah.conf.example"
install -Dm644 /dev/null "$STAGE/usr/lib/systemd/system/yaah-server@.service"

cat > "$STAGE/etc/yaah/yaah.conf.example" <<'EOF'
# YAAH headless server configuration (loaded by the systemd unit).
# Copy this file to yaah.conf and set the passphrase desktop clients
# must present. Without a yaah.conf the daemon runs and is discoverable
# on the LAN but refuses every remote request.
#
#   sudo cp /etc/yaah/yaah.conf.example /etc/yaah/yaah.conf
#   sudo sed -i 's/^#YAAH_PASSPHRASE=/YAAH_PASSPHRASE=/' /etc/yaah/yaah.conf
#   sudo chmod 640 /etc/yaah/yaah.conf   # root-readable + your user only
#   sudo systemctl restart yaah-server@<youruser>
#
# Everything else (the rest of ~/.yaah/config.json) stays in the home
# directory of the user the daemon runs as.

#YAAH_PASSPHRASE=
EOF

cat > "$STAGE/usr/lib/systemd/system/yaah-server@.service" <<'EOF'
[Unit]
Description=YAAH headless server (workspace: %i's home)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=%i
# - prefix: missing file is fine (daemon runs with no passphrase, remote
# exec refused). Format: YAAH_PASSPHRASE=secret (see yaah.conf.example).
EnvironmentFile=-/etc/yaah/yaah.conf
ExecStart=/usr/bin/yaah-server
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

mkdir -p "$STAGE/DEBIAN"

cat > "$STAGE/DEBIAN/control" <<EOF
Package: yaah-server
Version: $VERSION
Section: net
Priority: optional
Architecture: all
Depends: systemd | systemd-tmpfiles
Maintainer: YAAH <yaah@localhost>
Description: YAAH headless server (remote-exec host for YAAH desktop clients)
 Runs the YAAH remote-exec API as a systemd daemon. Conversations and
 provider keys stay on the desktop client; this host only executes
 workspace tools (shell, files, git) in the login user's home directory.
 Configure the passphrase in /etc/yaah/yaah.conf (see the .example file).
EOF

cat > "$STAGE/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = configure ] && command -v systemctl >/dev/null 2>&1; then
  systemctl daemon-reload || true
  # First real login user (UID 1000-59999, home dir, login shell).
  USER=$(getent passwd | awk -F: '$3 >= 1000 && $3 < 60000 && $6 != "" && $7 !~ /(nologin|false)$/ {print $1; exit}')
  if [ -n "$USER" ]; then
    # deb-systemd-invoke rejects combined flags (`enable --now`) on some
    # releases ("Unknown option: now") — enable and start separately.
    deb-systemd-invoke enable "yaah-server@$USER.service" || true
    deb-systemd-invoke start "yaah-server@$USER.service" || true
    echo "yaah-server: enabled + started yaah-server@$USER"
    echo "  configure the passphrase: see /etc/yaah/yaah.conf.example"
    echo "  other users: sudo systemctl enable --now yaah-server@<user>"
  else
    echo "yaah-server: no login user found; enable with: sudo systemctl enable --now yaah-server@<user>"
  fi
fi
EOF

cat > "$STAGE/DEBIAN/prerm" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = remove ] && command -v systemctl >/dev/null 2>&1; then
  # Stop every per-user instance of the template unit.
  systemctl list-units 'yaah-server@*.service' --no-legend 2>/dev/null | \
    awk '{print $1}' | xargs -r deb-systemd-invoke stop || true
  systemctl list-unit-files 'yaah-server@*.service' --no-legend 2>/dev/null | \
    awk '{print $1}' | xargs -r deb-systemd-invoke disable || true
fi
EOF

chmod 755 "$STAGE/DEBIAN/postinst" "$STAGE/DEBIAN/prerm"

mkdir -p dist
dpkg-deb --build --root-owner-group "$STAGE" "dist/yaah-server_${VERSION}_all.deb"
echo "deb: dist/yaah-server_${VERSION}_all.deb"
