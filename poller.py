#!/usr/bin/env python3

import argparse        # INPUT: read CLI args (--config, --out, --log-level)
import json            # OUTPUT: write results as JSON
import logging         # OUTPUT: logs to stderr (INFO/WARNING/ERROR)
import subprocess      # SNMP: run the Linux command "snmpget"
import time            # Timing: durations + per-target budget
from datetime import datetime, UTC   # OUTPUT: timestamp in JSON metadata
from pathlib import Path             # OUTPUT: config filename in JSON metadata
import yaml            # INPUT: read config.yml (YAML -> dict)


# INPUT: load YAML config file into a Python dict
def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)     # OUTPUT: cfg dict


# INPUT validation: fail fast if config is malformed (prevents confusing runtime errors)
def validate_config(cfg: dict) -> None:
    # INPUT: cfg must have these top-level keys
    if "defaults" not in cfg or "targets" not in cfg:
        raise ValueError("Config must contain 'defaults' and 'targets'")

    defaults = cfg["defaults"]       # INPUT: shared settings
    targets = cfg["targets"]         # INPUT: list of devices to poll

    # INPUT: required defaults (drive program behavior)
    required = ["snmp_version", "timeout_s", "retries", "target_budget_s", "oids"]
    for key in required:
        if key not in defaults:
            raise ValueError(f"'defaults' missing: {key}")

    # INPUT: must have OIDs and at least 2 targets (lab requirement)
    if not isinstance(defaults["oids"], list) or len(defaults["oids"]) < 1:
        raise ValueError("'defaults.oids' must be a non-empty list")
    if not isinstance(targets, list) or len(targets) < 2:
        raise ValueError("'targets' must be a list with at least 2 targets")

    # INPUT: numeric checks (crashes here -> config is wrong)
    float(defaults["timeout_s"])
    int(defaults["retries"])
    float(defaults["target_budget_s"])

    # INPUT: validate each target entry
    for t in targets:
        if not isinstance(t, dict):
            raise ValueError("Each target must be a dict")
        if "name" not in t or "ip" not in t:
            raise ValueError("Each target must contain 'name' and 'ip'")

        # INPUT: community can be per-target OR in defaults
        if "community" not in t and "community" not in defaults:
            raise ValueError(f"Target '{t.get('name')}' needs community (target or defaults)")

        # INPUT: optional per-target OIDs must be a list
        if "oids" in t and not isinstance(t["oids"], list):
            raise ValueError(f"Target '{t.get('name')}' has invalid 'oids' (must be list)")


# INPUT merge: combine defaults + target into one ready-to-poll dict (less duplication in YAML)
def merge_defaults(defaults: dict, target: dict) -> dict:
    merged = {
        "name": target["name"],
        "ip": target["ip"],
        "community": target.get("community", defaults.get("community")),  # INPUT: choose community
        "timeout_s": float(defaults["timeout_s"]),
        "retries": int(defaults["retries"]),
        "target_budget_s": float(defaults["target_budget_s"]),
        "oids": list(defaults["oids"]),                                   # INPUT: base OID list
    }

    # INPUT: optional extra OIDs per target (no duplicates)
    if "oids" in target:
        for oid in target["oids"]:
            if oid not in merged["oids"]:
                merged["oids"].append(oid)

    return merged  # OUTPUT: merged target dict


# SNMP: build the exact snmpget command for one OID
def build_snmpget_cmd(target: dict, oid: str) -> list[str]:
    # OUTPUT: list of args passed to subprocess.run(...)
    return [
        "snmpget",
        "-v2c",
        "-c", target["community"],
        "-r", "0",      # retries handled by OUR code (not snmpget)
        "-Oqv",         # value only => easier JSON
        target["ip"],
        oid,
    ]


# SNMP: run one request and classify result (ok/value or error type)
def run_snmpget(cmd: list[str], timeout_s: float):
    start = time.time()  # timing this single request
    try:
        # INPUT: timeout_s limits how long we wait for snmpget
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
        elapsed = time.time() - start

        # OUTPUT: success => return the value (already trimmed)
        if p.returncode == 0:
            return True, p.stdout.strip(), elapsed

        # OUTPUT: failure => classify error (used by retry logic)
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


# POLL: one target (all OIDs), with retries + per-target budget
def poll_target(target: dict) -> dict:
    name = target["name"]
    ip = target["ip"]
    retries = target["retries"]
    budget_s = target["target_budget_s"]
    timeout_s = target["timeout_s"]
    oids = target["oids"]

    # INFO: normal progress (more descriptive)
    logging.info(
        "Target start: %s (%s). Will poll %d OIDs. timeout=%.1fs, retries=%d, budget=%.1fs",
        name, ip, len(oids), timeout_s, retries, budget_s
    )

    start = time.time()             # start time for THIS target
    deadline = start + budget_s     # INPUT: time budget per target (prevents hanging)

    oid_results = {}  # OUTPUT: per OID result (ok/value or ok/error)
    ok_count = 0
    fail_count = 0

    for oid in oids:
        # WARNING: budget exceeded => stop polling this target, continue with next target
        if time.time() >= deadline:
            oid_results[oid] = {"ok": False, "error": "budget_exceeded"}
            fail_count += 1
            logging.warning(
                "Budget exceeded: target=%s (%s) reached %.1fs budget. "
                "Stopping this target early so a slow/down device does NOT block the whole run.",
                name, ip, budget_s
            )
            break

        attempt = 0
        while True:
            attempt += 1

            # INPUT: build cmd for this target+OID, then run it with timeout
            cmd = build_snmpget_cmd(target, oid)
            ok, value_or_error, _elapsed = run_snmpget(cmd, timeout_s)

            if ok:
                oid_results[oid] = {"ok": True, "value": value_or_error}
                ok_count += 1
                break

            # ERROR: auth failures are serious and retrying won't help
            if value_or_error == "auth":
                oid_results[oid] = {"ok": False, "error": "auth"}
                fail_count += 1
                logging.error(
                    "Authentication failure: target=%s (%s), oid=%s. "
                    "Most likely wrong community or SNMP ACL/permissions. "
                    "Retries will NOT help, so we fail fast for this OID.",
                    name, ip, oid
                )
                break

            # WARNING: retry only for timeout/unreachable (can be temporary)
            if value_or_error in ("timeout", "unreachable") and attempt <= retries:
                logging.warning(
                    "Temporary issue: target=%s (%s), oid=%s, reason=%s. "
                    "Retrying now (%d/%d).",
                    name, ip, oid, value_or_error, attempt, retries
                )
                continue

            # Final failure after retries (or non-retryable error)
            oid_results[oid] = {"ok": False, "error": value_or_error}
            fail_count += 1
            logging.warning(
                "Request failed: target=%s (%s), oid=%s, reason=%s, total_attempts=%d. "
                "Marking this OID failed and continuing with the next OID/target.",
                name, ip, oid, value_or_error, attempt
            )
            break

    duration = time.time() - start  # OUTPUT: target runtime (seconds)

    # OUTPUT: status is easier to interpret than only True/False
    if ok_count == len(oids) and fail_count == 0:
        status = "ok"
    elif ok_count > 0:
        status = "partial"
    else:
        status = "failed"

    # INFO: normal progress summary (more descriptive)
    logging.info(
        "Target end: %s (%s). status=%s, ok=%d, fail=%d, duration=%.3fs",
        name, ip, status, ok_count, fail_count, duration
    )

    return {
        "name": name,
        "ip": ip,
        "status": status,
        "oid_results": oid_results,
        "ok_count": ok_count,
        "fail_count": fail_count,
        "duration_s": round(duration, 3),
    }


# OUTPUT: configure which log messages are shown
def setup_logging(level_name: str) -> None:
    # INFO => INFO+WARNING+ERROR, WARNING => WARNING+ERROR (standard logging behavior)
    level = getattr(logging, level_name.upper(), logging.INFO)
    logging.basicConfig(level=level, format="%(levelname)s %(message)s")


def main() -> int:
    # INPUT: CLI arguments (how to run the poller)
    parser = argparse.ArgumentParser(description="Simple SNMP poller")
    parser.add_argument("--config", required=True, help="Path to config.yml")            # INPUT: YAML path
    parser.add_argument("--out", required=True, help='Output JSON file, or "-"')        # OUTPUT: destination
    parser.add_argument("--log-level", default="INFO", choices=["INFO", "WARNING"])     # OUTPUT: log verbosity
    args = parser.parse_args()

    setup_logging(args.log_level)

    run_start = time.time()  # start time for the whole run

    # INPUT: read + validate config (ERROR => exit 2)
    try:
        cfg = load_config(args.config)
        validate_config(cfg)
    except Exception as e:
        logging.error("Invalid configuration: %s", e)
        out = {"ok": False, "error": "config_invalid", "details": str(e)}  # OUTPUT: JSON error
        if args.out == "-":
            print(json.dumps(out, indent=2))
        else:
            with open(args.out, "w", encoding="utf-8") as f:
                json.dump(out, f, indent=2)
        return 2

    defaults = cfg["defaults"]   # INPUT: shared values
    targets = cfg["targets"]     # INPUT: device list

    # INFO: run-level summary (more descriptive)
    logging.info(
        "Run start: config=%s, targets=%d, output=%s, log_level=%s",
        args.config, len(targets), args.out, args.log_level
    )

    results = []         # OUTPUT: list of per-target results
    any_data = False     # OUTPUT: used for exit codes
    any_errors = False   # OUTPUT: used for exit codes

    for t in targets:
        # INPUT: merge defaults + target to get a full target config
        merged_target = merge_defaults(defaults, t)

        # OUTPUT: poll one target and store its result
        r = poll_target(merged_target)
        results.append(r)

        # OUTPUT: track overall success level
        if r["ok_count"] > 0:
            any_data = True
        if r["fail_count"] > 0:
            any_errors = True

    run_duration = time.time() - run_start  # OUTPUT: full run duration

    # OUTPUT: final JSON payload (run metadata + per-target results)
    out = {
        "run": {
            "timestamp": datetime.now(UTC).isoformat(),
            "config_file": Path(args.config).name,
            "duration_s": round(run_duration, 3),
        },
        "ok": (any_data and not any_errors),
        "targets": results,
    }

    # OUTPUT: write JSON to stdout or file
    if args.out == "-":
        print(json.dumps(out, indent=2))
    else:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=2)

    # OUTPUT: exit codes (automation-friendly)
    if not any_data:
        return 2
    if any_errors:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
