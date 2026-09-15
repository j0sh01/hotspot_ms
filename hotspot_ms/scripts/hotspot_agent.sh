#!/bin/sh
# hotspot_agent.sh - the one router-side agent for hotspot_ms.
#
# One cycle is one API call. The agent reports the raw openNDS client state and
# Frappe replies with the actions to take:
#
#   authorize   a client that reconnected (or rebooted) while still owning a
#               valid voucher, re-authenticated for its remaining minutes
#   deauthorize a client whose session was closed by expiry, data limit or an
#               admin kick
#
# Understanding openNDS's JSON lives on the server, where it is version
# controlled and testable. This script deliberately parses nothing: the older
# workers each parsed ndsctl output with awk, assumed a pretty-printed layout,
# and read the API response envelope wrong, so they quietly never worked.
#
# Config: /etc/hotspot.conf (written by the provisioning script, mode 0600).
#   PORTAL_URL     base URL of the Frappe portal, e.g. https://portal.example.com
#   NAS_IDENTIFIER the Nas Device name in Frappe
#   NAS_SECRET     the Nas Device shared secret
#
# Usage:
#   hotspot_agent.sh              run forever
#   hotspot_agent.sh --once       run a single cycle (handy for testing)
#   hotspot_agent.sh --verbose    log every cycle, not only changes

set -u

CONF=/etc/hotspot.conf
[ -f "$CONF" ] && . "$CONF"

PORTAL_URL="${PORTAL_URL:-}"
NAS_IDENTIFIER="${NAS_IDENTIFIER:-}"
NAS_SECRET="${NAS_SECRET:-}"
POLL_INTERVAL="${POLL_INTERVAL:-20}"
LOG_TAG="${LOG_TAG:-hotspot-agent}"

log() {
	if command -v logger >/dev/null 2>&1; then
		logger -t "$LOG_TAG" -- "$*"
	else
		echo "$LOG_TAG: $*"
	fi
}

die() {
	log "FATAL: $*"
	echo "$LOG_TAG: $*" >&2
	exit 1
}

urlencode() {
	# BusyBox-safe and fast. Inputs are MACs, IPs and compact JSON, which
	# contain no reserved characters except a handful - and spawning a process
	# per character made every sync cycle crawl on big client lists.
	# Escape only what form decoding would otherwise mangle: everything else
	# ({}":, etc.) passes through form-urlencoded bodies untouched.
	awk -v s="$1" 'BEGIN {
		gsub(/%/, "%25", s)
		gsub(/&/, "%26", s)
		gsub(/=/, "%3D", s)
		gsub(/\+/, "%2B", s)
		gsub(/#/, "%23", s)
		gsub(/\047/, "%27", s)
		gsub(/ /, "%20", s)
		printf "%s", s
	}'
}

jsonfield() {
	printf '%s' "$1" | jsonfilter -e "$2" 2>/dev/null || true
}

api() {
	wget -qO- --timeout=15 --post-data="$2" "$PORTAL_URL/api/method/hotspot_ms.api.portal.$1" 2>/dev/null || true
}

# Frappe wraps whitelisted API responses in an HTTP envelope:
#   {"message": {...}}
# jsonfilter on the raw body would look for .ok at the top level and always
# come back empty, which the older agent read as "portal rejected the sync".
# Unwrap the envelope first, then index into the real payload.
respmsg() {
	printf '%s' "$1" | jsonfilter -e '@.message' 2>/dev/null || true
}

auth_query() {
	printf 'nas_identifier=%s&secret=%s' \
		"$(urlencode "$NAS_IDENTIFIER")" "$(urlencode "$NAS_SECRET")"
}

acknowledge() {
	local session_id="$1" note="$2"
	api acknowledge_disconnect_action \
		"$(auth_query)&session_id=$(urlencode "$session_id")&result=ok&note=$(urlencode "$note")" >/dev/null
}

apply_authorize() {
	local response="$1" count="$2" i=0 mac minutes session_id
	while [ "$i" -lt "$count" ]; do
		mac="$(jsonfield "$response" "@.authorize[$i].mac_address")"
		minutes="$(jsonfield "$response" "@.authorize[$i].minutes")"
		session_id="$(jsonfield "$response" "@.authorize[$i].session_id")"
		i=$((i + 1))

		[ -n "$mac" ] || continue
		case "$minutes" in
		'' | *[!0-9]*) minutes=1 ;;
		esac
		[ "$minutes" -gt 0 ] || minutes=1

		if ndsctl auth "$mac" "$minutes" >/dev/null 2>&1; then
			log "authorized $mac for ${minutes}m (session $session_id)"
		else
			log "FAILED to authorize $mac (session $session_id)"
		fi
	done
}

apply_deauthorize() {
	local response="$1" count="$2" i=0 mac session_id note
	while [ "$i" -lt "$count" ]; do
		mac="$(jsonfield "$response" "@.deauthorize[$i].mac_address")"
		session_id="$(jsonfield "$response" "@.deauthorize[$i].session_id")"
		i=$((i + 1))

		[ -n "$session_id" ] || continue
		[ -n "$mac" ] || continue

		note="deauth_ok"
		if ! ndsctl deauth "$mac" >/dev/null 2>&1; then
			note="already_offline"
		fi
		log "deauthorized $mac (session $session_id, $note)"

		# Always acknowledge: unacked actions are handed out again every cycle.
		acknowledge "$session_id" "$note"
	done
}
	cycle() {
	local state response msg ok authorize_count deauthorize_count updated

	state="$(ndsctl json 2>/dev/null || true)"
	if [ -z "$state" ]; then
		[ "$VERBOSE" = "1" ] && log "openNDS is not answering, skipping cycle"
		return 0
	fi

	response="$(api router_sync "$(auth_query)&router_clients=$(urlencode "$state")")"
	if [ -z "$response" ]; then
		log "portal unreachable, skipping cycle"
		return 0
	fi

	msg="$(respmsg "$response")"
	if [ -z "$msg" ]; then
		log "portal returned no message envelope, skipping cycle"
		return 0
	fi

	ok="$(jsonfield "$msg" '@.ok')"
	if [ "$ok" != "true" ]; then
		log "portal rejected the sync: $msg"
		return 0
	fi

	authorize_count="$(jsonfield "$msg" '@.authorize_count')"
	deauthorize_count="$(jsonfield "$msg" '@.deauthorize_count')"
	updated="$(jsonfield "$msg" '@.updated_sessions')"
	case "$authorize_count" in '' | *[!0-9]*) authorize_count=0 ;; esac
	case "$deauthorize_count" in '' | *[!0-9]*) deauthorize_count=0 ;; esac
	case "$updated" in '' | *[!0-9]*) updated=0 ;; esac

	[ "$authorize_count" -gt 0 ] && apply_authorize "$msg" "$authorize_count"
	[ "$deauthorize_count" -gt 0 ] && apply_deauthorize "$msg" "$deauthorize_count"

	if [ "$VERBOSE" = "1" ] || [ "$updated" -gt 0 ] || [ "$authorize_count" -gt 0 ] || [ "$deauthorize_count" -gt 0 ]; then
		log "sync ok (clients $(jsonfield "$msg" '@.client_count'), usage $updated, auth $authorize_count, deauth $deauthorize_count)"
	fi
}

VERBOSE=0
case "${1:-}" in
--once) ONCE=1 ;;
--verbose) VERBOSE=1; ONCE=0 ;;
*) ONCE=0 ;;
esac

command -v ndsctl >/dev/null 2>&1 || die "ndsctl not found"
command -v wget >/dev/null 2>&1 || die "wget not found"
command -v jsonfilter >/dev/null 2>&1 || die "jsonfilter not found"
[ -n "$PORTAL_URL" ] || die "PORTAL_URL is not set (see $CONF)"
[ -n "$NAS_IDENTIFIER" ] || die "NAS_IDENTIFIER is not set (see $CONF)"
[ -n "$NAS_SECRET" ] || die "NAS_SECRET is not set (see $CONF)"

if [ "$ONCE" = "1" ]; then
	cycle
	exit 0
fi

log "started (nas=$NAS_IDENTIFIER portal=$PORTAL_URL every ${POLL_INTERVAL}s)"
while true; do
	cycle
	sleep "$POLL_INTERVAL"
done
