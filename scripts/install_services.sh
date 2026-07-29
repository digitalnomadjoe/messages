#!/bin/sh
# Install (or refresh) the BRITTLE message-bus systemd user units.
#
# Units are COPIED into ~/.config/systemd/user/, not symlinked.
#
# Why: when a unit file in the search path is itself a symlink, `systemctl
# --user disable <unit>` removes that symlink along with the wants/ entry. The
# unit then vanishes -- `systemctl enable --now` afterwards fails with "Unit
# ... could not be found", and a plain `restart` silently leaves the service
# inactive. That cost a live certification run once; copies make `disable` a
# safe, reversible operation.
#
# Re-run this script after editing anything in systemd/.
#
#   sh scripts/install_services.sh
#
# No sudo required.

set -eu

SRC="$(cd "$(dirname "$0")/../systemd" && pwd)"
DEST="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

UNITS="brittle-message-reviewer.service
brittle-browser-bridge.service
brittle-messages-locomotion.service
brittle-messages-control.service
brittle-message-sync.service
brittle-message-sync.timer"

mkdir -p "$DEST"

for u in $UNITS; do
  if [ ! -f "$SRC/$u" ]; then
    echo "missing source unit: $SRC/$u" >&2
    exit 1
  fi
  # Remove a stale symlink from an older install before copying over it.
  [ -L "$DEST/$u" ] && rm -f "$DEST/$u"
  cp -f "$SRC/$u" "$DEST/$u"
  echo "installed $u"
done

systemctl --user daemon-reload

# Restart whatever is already running. A long-lived service keeps executing the
# code it started with, so adding a protocol field (a new frontmatter key, say)
# wedges every running service until it is restarted -- it rejects the new
# messages as invalid on every pass. That failure is silent unless you read the
# journal, so refreshing units always restarts them.
for u in $UNITS; do
  case "$u" in *.timer) continue ;; esac
  if systemctl --user is-active --quiet "$u"; then
    systemctl --user restart "$u"
    echo "restarted $u (was running)"
  fi
done

echo
echo "Units installed to $DEST (as copies)."
echo "Enable what you want, e.g.:"
echo "  systemctl --user enable --now brittle-messages-locomotion.service"
echo "  systemctl --user enable --now brittle-message-sync.timer"
echo "  systemctl --user enable --now brittle-message-reviewer.service   # billable"
