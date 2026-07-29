#!/bin/sh
# Notification adapter for the BRITTLE message bus.
#
# Invoked by messagesctl / reviewer_daemon with these environment variables
# (never as command-line arguments, and never carrying a credential):
#
#   BRITTLE_ESCALATION_ID       message id of the escalation
#   BRITTLE_ESCALATION_SUMMARY  the one concrete question for Joe
#   BRITTLE_ESCALATION_LANE     locomotion | control
#   BRITTLE_ESCALATION_UNIT     active BRITTLE unit
#   BRITTLE_ESCALATION_PATH     path of the escalation inside the repo
#
# Exit 0 means "the notification was handed off successfully". The caller
# records that as notification_status=sent. Any other exit records `failed`.
# Do not exit 0 unless something really was delivered.
#
# TIER 1 (verified on this host): desktop notification via notify-send, plus a
#        durable append-only log that survives a missed popup.
# TIER 2 (not configured): true remote push to Joe's phone. See README ->
#        "Escalation workflow". Drop a webhook/SMS command in below and this
#        script will use it.

set -u

LOG_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/brittle-messages"
LOG="$LOG_DIR/escalations.log"
mkdir -p "$LOG_DIR"

TITLE="BRITTLE ${BRITTLE_ESCALATION_LANE:-?} needs Joe"
BODY="${BRITTLE_ESCALATION_SUMMARY:-(no summary)}
unit: ${BRITTLE_ESCALATION_UNIT:-?}
id:   ${BRITTLE_ESCALATION_ID:-?}"

# Durable record first -- this must never be the thing that fails.
printf '%s\t%s\t%s\t%s\t%s\n' \
  "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  "${BRITTLE_ESCALATION_ID:-?}" \
  "${BRITTLE_ESCALATION_LANE:-?}" \
  "${BRITTLE_ESCALATION_UNIT:-?}" \
  "${BRITTLE_ESCALATION_SUMMARY:-?}" >> "$LOG"

delivered=1

# --- Tier 2: remote push (configure to reach Joe when he is away) -----------
# Set BRITTLE_REMOTE_NOTIFY to a command that takes the message on stdin.
# Examples:
#   export BRITTLE_REMOTE_NOTIFY='curl -fsS -d @- ntfy.sh/your-private-topic'
#   export BRITTLE_REMOTE_NOTIFY='/home/robojoe/bin/sms-joe'
if [ -n "${BRITTLE_REMOTE_NOTIFY:-}" ]; then
  if printf '%s\n%s\n' "$TITLE" "$BODY" | sh -c "$BRITTLE_REMOTE_NOTIFY" >/dev/null 2>&1; then
    delivered=0
  fi
fi

# --- Tier 1: desktop notification at the workstation -----------------------
if command -v notify-send >/dev/null 2>&1; then
  if notify-send --urgency=critical --app-name="BRITTLE" "$TITLE" "$BODY" 2>/dev/null; then
    delivered=0
  fi
fi

exit "$delivered"
