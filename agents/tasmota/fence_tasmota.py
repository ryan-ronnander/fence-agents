#!@PYTHON@ -tt

# fence_tasmota - power fencing via Tasmota (and OpenBeken) smart plugs, relays, and power strips.
#
# This agent is intended for home labs and consumer hardware, where the fence device is merely a smart
# plug on WiFi. It fences by cutting power to the node, similar to a PDU fencing agent.
#
# The smart plug can only cut power temporarily and restore it for a typical fence reboot operation, so:
#   - CRUCIAL: the node's BIOS/UEFI must be set to power on after power loss ("Restore on AC Power Loss"
#     or the vendor's wording); otherwise a fenced node stays off until someone powers it on.
#   - do not put a UPS or any battery between the plug and the node.
#   - not intended for dual-PSU servers unless one smart plug is upstream of both supplies.
#
# Tasmota exposes a local HTTP command API at /cm?cmnd=<command>. Two commands do the work: "Power<n>"
# reads relay n and "Power<n> ON|OFF" sets it. The fencing framework (fence_action) provides
# on/off/reboot/status/monitor/metadata, retries, the power-wait loop, and the fence-race delay.
# Authentication is Tasmota's WebPassword, sent as user=admin&password=... query parameters (the user
# is always admin). The API is HTTP only: Tasmota's TLS support covers MQTT, not its web server.
# OpenBeken (BK72xx and similar) implements the same /cm API and works unchanged.
#
# Tasmota settings for a fence plug. The relay must change only when this agent says so.
#   Set:
#     PowerOnState 1    relay ON after the plug itself loses power (an outage), so nodes come back
#     SetOption73 1     detach the button, so a press cannot toggle power
#     a static IP, or a DHCP reservation: the agent targets a fixed --ip
#   Do not set:
#     PowerOnState 4    locks the relay ON: OFF is ignored and the plug cannot fence
#     PowerLock 1       same effect, freezes the relay
#     PulseTime         auto-reverts the relay after switching: a reboot's recovery ON undoes itself
#     Timers or Rules that touch the relay: a scheduled or rule-driven switch can re-power a node you
#                       just fenced, or cut one that already recovered
#   The agent does not verify these; a misconfigured plug fails the fence when the relay does not
#   confirm the requested state.

import sys
import io
import json
import logging
import atexit
import pycurl
from urllib.parse import quote

sys.path.append("@FENCEAGENTSLIBDIR@")
from fencing import *
from fencing import fail, fail_usage, run_delay, EC_STATUS, EC_LOGIN_DENIED


def relay(opt):
	# --plug is the 1-based relay number: 1 for a single-relay plug, N for outlet N of a strip.
	return int(opt.get("--plug") or 1)


def cmnd(conn, command):
	path = "/cm?cmnd=" + quote(command)
	if conn.pw:
		path += "&user=admin&password=" + quote(conn.pw)
	conn.setopt(pycurl.URL, (conn.base_url + path).encode("ascii"))
	buf = io.BytesIO()
	conn.setopt(pycurl.WRITEFUNCTION, buf.write)
	conn.perform()
	rc = conn.getinfo(pycurl.HTTP_CODE)
	body = buf.getvalue().decode("UTF-8").strip()
	buf.close()
	# Log the command, not the URL: the URL carries the password.
	logging.debug("cmnd %s -> %s %s", command, rc, body)
	if rc != 200:
		raise Exception("HTTP {} for cmnd {}: {}".format(rc, command, body))
	r = json.loads(body) if body[:1] == "{" else {}
	# Current firmware answers bad credentials with 401 (raised above). Older firmware answers 200 with
	# a WARNING body, which would otherwise be misread as a relay state.
	if "WARNING" in r:
		raise Exception("Tasmota auth failed: {}".format(r["WARNING"]))
	# A relay the device does not have, or a command the firmware lacks, comes back as HTTP 200 with
	# {"Command":"Error"} or {"Command":"Unknown"}. That must be a failure, never something to read a state from.
	if r.get("Command") in ("Error", "Unknown"):
		raise Exception("Tasmota rejected cmnd {}: {}".format(command, r))
	return r


def get_power_status(conn, opt):
	try:
		n = relay(opt)
		r = cmnd(conn, "Power%d" % n)
		# Tasmota answers {"POWER":"ON"} for a single relay and {"POWER2":"OFF"} for relay 2 of a strip;
		# OpenBeken answers with every channel. Only an explicit ON/OFF for this relay counts. Anything else
		# is a failure: a fence agent must never report "off" for a state it does not actually know.
		val = str(r.get("POWER%d" % n, r.get("POWER") if n == 1 else None)).upper()
		if val not in ("ON", "OFF"):
			raise Exception("no ON/OFF state for relay {} in {}".format(n, r))
		return val.lower()
	except Exception as e:
		logging.error("status failed: %s", e)
		fail(EC_STATUS)


def set_power_status(conn, opt):
	try:
		cmnd(conn, "Power%d %s" % (relay(opt), opt["--action"].upper()))
	except Exception as e:
		logging.debug("set failed: %s", e)
		fail(EC_STATUS)


def connect(opt):
	conn = pycurl.Curl()
	conn.base_url = "http://" + opt["--ip"] + ":" + str(opt["--ipport"])
	conn.pw = opt.get("--password")
	conn.setopt(pycurl.TIMEOUT, int(opt["--shell-timeout"]))
	conn.setopt(pycurl.HTTPHEADER, ["Accept: application/json"])
	# Probe up front so an unreachable plug or a bad password fails as a login error, not mid-fence.
	try:
		cmnd(conn, "Power%d" % relay(opt))
	except Exception as e:
		logging.error("cannot reach/authenticate to %s: %s", opt["--ip"], e)
		fail(EC_LOGIN_DENIED)
	return conn


def main():
	# no_password: Tasmota's WebPassword is optional, and the user is fixed to admin, so there is no --username.
	device_opt = ["ipaddr", "passwd", "no_password", "web", "port"]

	atexit.register(atexit_handler)

	all_opt["port"]["help"] = "-n, --plug=[n]                 Relay number: 1 for a single-relay plug, N for outlet N of a strip (default 1)"
	all_opt["port"]["shortdesc"] = "Relay number: 1 for a single-relay plug, N for outlet N of a strip."
	all_opt["port"]["required"] = "0"
	all_opt["port"]["default"] = "1"
	all_opt["shell_timeout"]["default"] = "5"
	all_opt["power_wait"]["default"] = "5"

	options = check_input(device_opt, process_input(device_opt))

	docs = {}
	docs["shortdesc"] = "Fence agent for Tasmota and OpenBeken smart plugs"
	docs["longdesc"] = (
		"fence_tasmota is a power fencing agent for smart plugs, relays, and power strips running Tasmota, "
		"controlled over the device's local HTTP command API (/cm). OpenBeken devices implement the same API "
		"and work unchanged. It is intended for home labs: it cuts power to the node, so the node only returns "
		"if its BIOS/UEFI is set to power on after power loss.")
	docs["vendorurl"] = "https://tasmota.github.io/docs/Commands/"
	show_docs(options, docs)

	# --plug must be a relay number. A node name here means Pacemaker was given no pcmk_host_map.
	if not str(options["--plug"]).isdigit() or int(options["--plug"]) < 1:
		fail_usage("--plug must be the relay number (1 for a single-relay plug); with Pacemaker, map node "
				   "names to relay numbers with pcmk_host_map")

	run_delay(options)
	conn = connect(options)
	atexit.register(conn.close)

	result = fence_action(conn, options, set_power_status, get_power_status)
	sys.exit(result)


if __name__ == "__main__":
	main()
