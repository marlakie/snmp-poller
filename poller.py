#!/usr/bin/env python3


# SNMP Poller
# What this script does:
#  1) Reads config.yml (YAML -> Python dict)
#  2) Validates that config contains required keys
#  3) For each target (device):
#       - Polls a list of OIDs using the Net-SNMP command "snmpget"
#       - Uses retries ONLY when the error is a timeout
#       - Stops polling that target if target_budget_s is exceeded
#  4) Prints JSON output (always)
#  5) Returns exit codes:
#       0 = all OK
#       1 = partial (some OK data but also errors)
#       2 = total fail (no data at all) OR config invalid

import json
import subprocess
import sys
import time
import yaml


# SECTION A: CONFIG HANDLING

def load_config(path="config.yml"):
    """
    PURPOSE:
      Read config.yml and convert YAML -> Python dictionary (dict).
    WHY:
      This makes the script config-driven (no hardcoded IPs/OIDs in code).
    """
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def validate_config(cfg):
    """
    PURPOSE:
      Check that config contains the keys we need.
    WHY:
      If config is wrong, we want a clear error and exit code 2.
      This prevents confusing runtime errors later.
    WHAT WE CHECK:
      - Top level: defaults + targets
      - defaults contains required settings
      - defaults.oids is a list
      - targets is a list with at least 2 entries
      - each target has name + ip
    """
    # Must contain these top-level keys
    if "defaults" not in cfg or "targets" not in cfg:
        raise ValueError("Config must contain 'defaults' and 'targets'")

    defaults = cfg["defaults"]
    targets = cfg["targets"]

    # Required keys in defaults
    required = ["snmp_version", "community", "timeout_s", "retries", "target_budget_s", "oids"]
    for k in required:
        if k not in defaults:
            raise ValueError(f"'defaults' missing: {k}")

    # OIDs must be a non-empty list
    if not isinstance(defaults["oids"], list) or len(defaults["oids"]) < 1:
        raise ValueError("'defaults.oids' must be a list with at least 1 OID")

    # Must have at least 2 targets
    if not isinstance(targets, list) or len(targets) < 2:
        raise ValueError("'targets' must be a list with at least 2 targets")

    # Each target must have name and ip
    for t in targets:
        if not isinstance(t, dict):
            raise ValueError("Each target must be a dict")
        if "name" not in t or "ip" not in t:
            raise ValueError("Each target must contain 'name' and 'ip'")

        # If a target has extra OIDs, they must be a list
        if "oids" in t and not isinstance(t["oids"], list):
            raise ValueError(f"Target '{t.get('name')}' has invalid 'oids' (must be a list)")



# SECTION B: SNMP FUNCTION (one request)
def snmpget_v2c(ip, community, oid, timeout_s):
    """
    PURPOSE:
      Poll ONE OID from ONE device using "snmpget".
    WHY:
      Lab recommends using Net-SNMP CLI tools via subprocess.

    COMMAND WE RUN:
      snmpget -v2c -c <community> -t <timeout> -r 0 -Oqv <ip> <oid>

    IMPORTANT FLAGS:
      -v2c     : SNMP version 2c
      -c       : community string (like a password for SNMPv2c)
      -t       : timeout per request (seconds)
      -r 0     : retries handled by OUR code, so snmpget itself should not retry
      -Oqv     : output only the VALUE (makes parsing easy for JSON)

    RETURNS:
      (True, value) on success
      (False, "timeout") if device does not respond
      (False, "auth") if community/auth is wrong
      (False, "snmp_error") for other errors
    """
    cmd = ["snmpget", "-v2c", "-c", community, "-t", str(timeout_s), "-r", "0", "-Oqv", ip, oid]
    p = subprocess.run(cmd, capture_output=True, text=True)

    # returncode 0 means success
    if p.returncode == 0:
        return True, p.stdout.strip()

    # if error, Net-SNMP usually writes to stderr
    err = (p.stderr or p.stdout or "").strip()

    # classify common errors so we can apply correct logic
    if "Timeout" in err or "No Response" in err:
        return False, "timeout"
    if "Authentication failure" in err or "authorizationError" in err:
        return False, "auth"

    return False, "snmp_error"


# SECTION C: POLL ONE TARGET (multiple OIDs)
def poll_target(target, defaults):
    """
    PURPOSE:
      Poll ONE device (target) for ALL OIDs.
    FEATURES:
      1) OID list = defaults.oids + optional target.oids
      2) target_budget_s = maximum time allowed for this device
         -> prevents a down device from blocking the entire run
      3) retries only on timeout:
         - timeout: retry up to <retries>
         - auth: fail-fast (retry doesn't help)
    """
    name = target["name"]
    ip = target["ip"]

    # Settings are read from config (config-driven)
    community = defaults["community"]
    timeout_s = float(defaults["timeout_s"])
    retries = int(defaults["retries"])
    budget_s = float(defaults["target_budget_s"])

    # Build OID list (keep defaults first, then target-specific, no duplicates)
    oids = list(defaults["oids"])
    if "oids" in target:
        for oid in target["oids"]:
            if oid not in oids:
                oids.append(oid)

    # Budget handling: we compute a deadline time
    start = time.time()
    deadline = start + budget_s

    data = {}      # on success: data[oid] = value
    errors = []    # on failure: {"oid": oid, "error": "..."} appended here

    # Poll OID by OID
    for oid in oids:

        # If our time budget for this target is over, stop immediately
        if time.time() >= deadline:
            errors.append({"oid": oid, "error": "budget_exceeded"})
            break

        attempt = 0
        while True:
            attempt += 1

            ok, val = snmpget_v2c(ip, community, oid, timeout_s)

            # SUCCESS: store value and go to next OID
            if ok:
                data[oid] = val
                break

            # AUTH ERROR: fail-fast (retries won't fix wrong community)
            if val == "auth":
                errors.append({"oid": oid, "error": "auth"})
                break

            # TIMEOUT: retry while attempt <= retries
            if val == "timeout" and attempt <= retries:
                continue

            # OTHER ERRORS (or no retries left): record error and stop retry loop
            errors.append({"oid": oid, "error": val})
            break

    duration = time.time() - start

    # ok_target means "no errors at all for this target"
    ok_target = (len(errors) == 0)

    # Return a JSON-friendly dict for this target
    return {
        "name": name,
        "ip": ip,
        "ok": ok_target,
        "duration_s": round(duration, 3),
        "data": data,
        "errors": errors,
    }

# SECTION D: MAIN (run everything + JSON output + exit codes)
def main():
    """
    PURPOSE:
      The main entry point.
      - reads config path from argv (default: config.yml)
      - validates config
      - polls all targets
      - prints JSON output
      - returns exit code 0/1/2
    """
    # Allow: ./poller.py config.yml
    cfg_path = "config.yml"
    if len(sys.argv) > 1:
        cfg_path = sys.argv[1]

    # Read + validate config
    try:
        cfg = load_config(cfg_path)
        validate_config(cfg)
    except Exception as e:
        # Config invalid -> JSON error + exit code 2
        print(json.dumps({"ok": False, "error": "config_invalid", "details": str(e)}, indent=2))
        return 2

    defaults = cfg["defaults"]
    targets = cfg["targets"]

    results = []
    any_data = False     # true if we got ANY SNMP values from any target
    any_errors = False   # true if ANY target had errors

    # Poll all targets one by one
    for t in targets:
        r = poll_target(t, defaults)
        results.append(r)

        if len(r["data"]) > 0:
            any_data = True
        if len(r["errors"]) > 0:
            any_errors = True

    # Print JSON output ALWAYS (even if partial)
    out = {"ok": (any_data and not any_errors), "results": results}
    print(json.dumps(out, indent=2))

    # Exit codes for automation (cron/CI):
    # 2 = total fail (no data at all)
    # 1 = partial (some data but also errors)
    # 0 = all ok
    if not any_data:
        return 2
    if any_errors:
        return 1
    return 0


# Standard Python pattern: run main() only when executed directly
if __name__ == "__main__":
    raise SystemExit(main())
