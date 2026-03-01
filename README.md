# Lab 5 — SNMP Poller (Python + Net-SNMP)

This project is a small SNMP poller written in Python.
It reads targets and OIDs from a YAML config file and polls devices using the `snmpget` CLI tool (SNMPv2c).

# Files
- `poller.py` — main poller script
- `config.yml` — config-driven targets + OIDs
- `test_config.py` — unit tests for config parsing/validation (no SNMP)
- `README.md` — how to run + expected output

# Requirements
- Ubuntu/Linux
- `snmpget` from Net-SNMP
- Python 3
- PyYAML

# Install (Ubuntu)
```bash
sudo apt-get update
sudo apt-get install -y snmp python3 python3-venv

python3 -m venv .venv
source .venv/bin/activate
pip install pyyaml
```

##Config format (config.yml)
defaults:
  snmp_version: "v2c"
  community: "public"
  timeout_s: 2.5
  retries: 1
  target_budget_s: 10
  oids:
    - "sysUpTime.0"
    - "sysName.0"
    - "sysDescr.0"
    - "sysLocation.0"

targets:
  - name: "Switch1"
    ip: "172.16.0.179"

  - name: "MyRouter"
    ip: "172.16.0.181"

  - name: "Switch2"
    ip: "172.16.0.235"

##Run unit tests
```bash
python3 -m unittest -v
```

#Run the Poller (print JSON to terminal)
```bash
python3 poller.py --config config.yml --out -
echo $?
```
##Run the poller (print JSON to file)
```bash
python3 poller.py --config config.yml --out out.json
echo $?
cat out.json
```

#The poller uses log levels:

INFO for normal run/target start and end
WARNING for retries, timeouts, and budget exceeded
ERROR for config errors and authentication errors

example: python3 poller.py --config config.yml --out out.json --log-level INFO

#Output format
The script writes JSON output with:
run
timestamp
config_file
duration_s
ok (overall run status)
targets[]
name
ip
status (ok, partial, or failed)
oid_results
ok_count
fail_count
duration_s

#Exit codes
0 = all OK

1 = partial success (some data collected, but there were errors)

2 = total failure (no data collected) or invalid config
