#!@PYTHON@ -tt

# fence_esphome - power fencing via an ESPHome relay (web_server REST API).
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
# The device must run ESPHome's web_server component. --plug is the relay's switch entity NAME as
# declared in the YAML (e.g. "Relay"), not its object_id: ESPHome 2026.7.0 dropped the object_id URL
# form (/switch/relay). GET /switch/<name> reads the relay; POST /switch/<name>/turn_on|turn_off sets
# it. If web_server has auth: configured, pass --password (and --username if it is not admin); basic
# and digest both work. The web_server is HTTP only. The fencing framework (fence_action) provides
# on/off/reboot/status/monitor/metadata, retries, the power-wait loop, and the fence-race delay.
#
# ESPHome settings for a fence plug. The relay must change only when this agent says so.
#   Set:
#     restore_mode: ALWAYS_ON   relay ON after the plug itself loses power (an outage), so nodes come
#                               back
#     web_server: auth:         optional; basic or digest, passed as --username (default admin) and
#                               --password
#     a static IP in the wifi: block, or a DHCP reservation: the agent targets a fixed --ip
#   Do not set:
#     automations on the relay  on_turn_on/on_turn_off with a delay, interlock, time or interval
#                               automations: any of them can re-power a node you just fenced or cut
#                               one that already recovered
#     the button on the relay   wire GPIO0 to a restart or an event, not to the relay
#   ESPHome exposes state but not configuration over HTTP, so the agent does not verify these; a
#   misconfigured plug fails the fence when the relay does not confirm the requested state.

import sys
import io
import json
import logging
import atexit
import pycurl
from urllib.parse import quote

sys.path.append("@FENCEAGENTSLIBDIR@")
from fencing import *
from fencing import fail, run_delay, EC_STATUS, EC_LOGIN_DENIED


def http(conn, path, post=False):
	conn.setopt(pycurl.URL, (conn.base_url + path).encode("ascii"))
	if post:
		conn.setopt(pycurl.POST, 1)
		conn.setopt(pycurl.POSTFIELDS, "")
	else:
		conn.setopt(pycurl.HTTPGET, 1)
	buf = io.BytesIO()
	conn.setopt(pycurl.WRITEFUNCTION, buf.write)
	conn.perform()
	rc = conn.getinfo(pycurl.HTTP_CODE)
	body = buf.getvalue().decode("UTF-8").strip()
	buf.close()
	logging.debug("%s %s -> %s %s", "POST" if post else "GET", path, rc, body)
	if rc != 200:
		raise Exception("HTTP {} from {}: {}".format(rc, path, body))
	return json.loads(body) if body[:1] == "{" else body


def switch_path(opt):
	return "/switch/" + quote(str(opt["--plug"]))


def get_power_status(conn, opt):
	try:
		r = http(conn, switch_path(opt))   # {"id":"switch/Relay","value":true,"state":"ON"}
		# Only an explicit ON/OFF counts. Anything else is a failure: a fence agent must never report
		# "off" for a state it does not actually know.
		state = str(r.get("state") if isinstance(r, dict) else r).upper()
		if state not in ("ON", "OFF"):
			raise Exception("no ON/OFF state for switch {} in {}".format(opt["--plug"], r))
		return state.lower()
	except Exception as e:
		logging.error("status failed: %s", e)
		fail(EC_STATUS)


def set_power_status(conn, opt):
	try:
		http(conn, switch_path(opt) + "/turn_" + opt["--action"], post=True)
	except Exception as e:
		logging.debug("set failed: %s", e)
		fail(EC_STATUS)


def connect(opt):
	conn = pycurl.Curl()
	conn.base_url = "http://" + opt["--ip"] + ":" + str(opt["--ipport"])
	conn.setopt(pycurl.TIMEOUT, int(opt["--shell-timeout"]))
	conn.setopt(pycurl.HTTPHEADER, ["Accept: application/json"])
	if opt.get("--password"):
		# web_server auth may be basic or digest; HTTPAUTH_ANY negotiates whichever the device offers.
		conn.setopt(pycurl.HTTPAUTH, pycurl.HTTPAUTH_ANY)
		conn.setopt(pycurl.USERPWD, "{}:{}".format(opt.get("--username") or "admin", opt["--password"]))
	# Probe up front so an unreachable device or bad credentials fail as a login error, not mid-fence.
	try:
		http(conn, switch_path(opt))
	except Exception as e:
		logging.error("cannot reach/authenticate to %s: %s", opt["--ip"], e)
		fail(EC_LOGIN_DENIED)
	return conn


def main():
	# no_login/no_password: web_server auth is optional; when it is on, --username defaults to admin.
	device_opt = ["ipaddr", "login", "no_login", "passwd", "no_password", "web", "port"]

	atexit.register(atexit_handler)

	all_opt["login"]["default"] = "admin"
	all_opt["port"]["help"] = "-n, --plug=[name]              Name of the relay's switch entity in the ESPHome YAML (e.g. Relay)"
	all_opt["port"]["shortdesc"] = "Name of the relay's switch entity as declared in the ESPHome YAML (e.g. Relay), not its object_id."
	all_opt["shell_timeout"]["default"] = "5"
	all_opt["power_wait"]["default"] = "5"

	options = check_input(device_opt, process_input(device_opt))

	docs = {}
	docs["shortdesc"] = "Fence agent for ESPHome relays"
	docs["longdesc"] = (
		"fence_esphome is a power fencing agent for smart plugs and relays running ESPHome, controlled over "
		"the device's local web_server REST API; --plug is the relay's switch entity name. It is intended for "
		"home labs: it cuts mains power to the node, so the node only returns if its BIOS/UEFI powers on after "
		"AC loss. ESPHome does not expose configuration over HTTP, so the relay cannot be checked for "
		"fence-defeating settings: give it restore_mode ALWAYS_ON and keep it free of automations, interlocks, "
		"and timers that could switch it on their own.")
	docs["vendorurl"] = "https://esphome.io/components/web_server.html"
	show_docs(options, docs)

	run_delay(options)
	conn = connect(options)
	atexit.register(conn.close)

	result = fence_action(conn, options, set_power_status, get_power_status)
	sys.exit(result)


if __name__ == "__main__":
	main()
