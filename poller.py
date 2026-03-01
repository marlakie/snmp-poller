#!/usr/bin/env python3

import argparse
import json
import logging
import subprocess
import time
from datetime import datetime, UTC
from pathlib import Path

import yaml


# Read YAML config file
def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# Validate config structure
def validate_config(cfg):
    if "defaults" not in cfg or "targets" not in cfg:
        raise ValueError("Config must contain 'defaults' and 'targets'")

    defaults = cfg["defaults"]
    targets = cfg["targets"]

    required = ["snmp_version", "timeout_s", "retries", "target_budget_s", "oids"]
    for key in required:
        if key not in defaults:
            raise ValueError(f"'defaults' missing: {key}")

    if not isinstance(defaults["oids"], list) or len(defaults["oids"]) < 1:
        raise ValueError("'defaults.oids' must be a non-empty list")

    if not isinstance(targets, list) or len(targets) < 2:
        raise ValueError("'targets' must be a list with at least 2 targets")

    # Basic type checks
    try:
        float(defaults["timeout_s"])
        int(defaults["retries"])
        float(defaults["target_budget_s"])
    except Exception:
        raise ValueError("timeout_s, retries and target_budget_s must be numeric")

    for t in targets:
        if not isinstance(t, dict):
            raise ValueError("Each target must be a dict")
        if "name" not in t or "ip" not in t:
            raise ValueError("Each target must contain 'name' and 'ip'")

        # Allow community either on target or in defaults
        if "community" not in t and "community" not in defaults:
            raise ValueError(
                f"Target '{t.get('name')}' must have 'community', or defaults must define it"
            )

        if "oids" in t and not isinstance(t["oids"], list):
            raise ValueError(f"Target '{t.get('name')}' has invalid 'oids'")


# Merge defaults with a single target
def merge_defaults(defaults, target):
    merged = {
        "name": target["name"],
        "ip": target["ip"],
        "community": target.get("community", defaults.get("community")),
        "snmp_version": defaults["snmp_version"],
        "timeout_s": float(defaults["timeout_s"]),
        "retries": int(defaults["retries"]),
        "target_budget_s": float(defaults["target_budget_s"]),
        "oids": list(defaults["oids"]),
    }

    # Add target-specific OIDs without duplicates
    if "oids" in target:
        for oid in target["oids"]:
            if oid not in merged["oids"]:
                merged["oids"].append(oid)

    return merged


# Build snmpget command
def build_snmpget_cmd(target, oid):
    return [
        "snmpget",
        "-v2c",
        "-c",
        target["community"],
        "-r",
        "0",           # retries handled by our code
        "-Oqv",        # value only
        target["ip"],
        oid,
    ]


# Run one SNMP request
def run_snmpget(cmd, timeout_s):
    start = time.time()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
        elapsed = time.time() - start

        if p.returncode == 0:
            return True, p.stdout.strip(), elapsed

        err = (p.stderr or p.stdout or "").strip()

        if "Timeout" in err or "No Response" in err:
            return False, "timeout", elapsed
        if "Authentication failure" in err or "authorizationError" in err:
            return False, "auth", elapsed
        if "Unknown host" in err or "Name or service not known" in err:
            return False, "unreachable", elapsed

        return False, "snmp_error", elapsed

    except subprocess.TimeoutExpired:
        elapsed = time.time() - start
        return False, "timeout", elapsed


# Poll one target
def poll_target(target):
    name = target["name"]
    ip = target["ip"]
    retries = target["retries"]
    budget_s = target["target_budget_s"]
    timeout_s = target["timeout_s"]
    oids = target["oids"]

    logging.info("Target start name=%s ip=%s oids=%d", name, ip, len(oids))

    start = time.time()
    deadline = start + budget_s

    oid_results = {}
    ok_count = 0
    fail_count = 0

    for oid in oids:
        if time.time() >= deadline:
            oid_results[oid] = {"ok": False, "error": "budget_exceeded"}
            fail_count += 1
            logging.warning("Budget exceeded for target=%s ip=%s", name, ip)
            break

        attempt = 0
        while True:
            attempt += 1
            cmd = build_snmpget_cmd(target, oid)
            ok, value_or_error, _elapsed = run_snmpget(cmd, timeout_s)

            if ok:
                oid_results[oid] = {"ok": True, "value": value_or_error}
                ok_count += 1
                break

            # Fail fast on auth
            if value_or_error == "auth":
                oid_results[oid] = {"ok": False, "error": "auth"}
                fail_count += 1
                logging.error("Auth failure target=%s ip=%s oid=%s", name, ip, oid)
                break

            # Retry only on timeout/unreachable
            if value_or_error in ("timeout", "unreachable") and attempt <= retries:
                logging.warning(
                    "Retry target=%s ip=%s oid=%s attempt=%d/%d reason=%s",
                    name, ip, oid, attempt, retries, value_or_error
                )
                continue

            oid_results[oid] = {"ok": False, "error": value_or_error}
            fail_count += 1

            if value_or_error in ("timeout", "unreachable"):
                logging.warning("Timeout/unreachable target=%s ip=%s oid=%s", name, ip, oid)
            else:
                logging.error(
                    "SNMP error target=%s ip=%s oid=%s err=%s",
                    name,
                    ip,
                    oid,
                    value_or_error,
                )
            break

    duration = time.time() - start

    if ok_count == len(oids) and fail_count == 0:
        status = "ok"
    elif ok_count > 0:
        status = "partial"
    else:
        status = "failed"

    logging.info("Target end name=%s ip=%s status=%s duration=%.3f", name, ip, status, duration)

    return {
        "name": name,
        "ip": ip,
        "status": status,
        "oid_results": oid_results,
        "ok_count": ok_count,
        "fail_count": fail_count,
        "duration_s": round(duration, 3),
    }


def setup_logging(level_name):
    level = getattr(logging, level_name.upper(), logging.INFO)
    logging.basicConfig(level=level, format="%(levelname)s %(message)s")


# Main function
def main():
    parser = argparse.ArgumentParser(description="Simple SNMP poller")
    parser.add_argument("--config", required=True, help="Path to config.yml")
    parser.add_argument("--out", required=True, help="Output JSON file, or - for stdout")
    parser.add_argument("--log-level", default="INFO", help="Logging level (INFO/WARNING/ERROR)")
    args = parser.parse_args()

    setup_logging(args.log_level)

    run_start = time.time()
    cfg_path = args.config

    try:
        cfg = load_config(cfg_path)
        validate_config(cfg)
    except Exception as e:
        logging.error("Config error: %s", e)
        out = {"ok": False, "error": "config_invalid", "details": str(e)}
        if args.out == "-":
            print(json.dumps(out, indent=2))
        else:
            with open(args.out, "w", encoding="utf-8") as f:
                json.dump(out, f, indent=2)
        return 2

    defaults = cfg["defaults"]
    targets = cfg["targets"]

    logging.info("Run start targets=%d out=%s", len(targets), args.out)

    results = []
    any_data = False
    any_errors = False

    for t in targets:
        merged_target = merge_defaults(defaults, t)
        r = poll_target(merged_target)
        results.append(r)

        if r["ok_count"] > 0:
            any_data = True
        if r["fail_count"] > 0:
            any_errors = True

    run_duration = time.time() - run_start

    out = {
        "run": {
            "timestamp": datetime.now(UTC).isoformat(),
            "config_file": str(Path(cfg_path).name),
            "duration_s": round(run_duration, 3),
        },
        "ok": (any_data and not any_errors),
        "targets": results,
    }

    if args.out == "-":
        print(json.dumps(out, indent=2))
    else:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=2)

    if not any_data:
        return 2
    if any_errors:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
