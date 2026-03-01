import unittest

from poller import load_config, validate_config


class TestConfig(unittest.TestCase):
    # Test 1: real config.yml loads and validates
    def test_config_loads_and_validates(self):
        cfg = load_config("config.yml")

        self.assertIsInstance(cfg, dict)
        self.assertIn("defaults", cfg)
        self.assertIn("targets", cfg)

        # Should not raise
        validate_config(cfg)

        self.assertGreaterEqual(len(cfg["targets"]), 2)
        self.assertIsInstance(cfg["defaults"]["oids"], list)
        self.assertGreaterEqual(len(cfg["defaults"]["oids"]), 1)

    # Test 2: broken config should fail
    def test_invalid_config_missing_keys(self):
        bad = {"hello": "world"}

        with self.assertRaises(ValueError):
            validate_config(bad)

    # Test 3: target-level community is also allowed
    def test_target_level_community_is_allowed(self):
        cfg = {
            "defaults": {
                "snmp_version": "v2c",
                "timeout_s": 2.5,
                "retries": 1,
                "target_budget_s": 10,
                "oids": ["sysUpTime.0"]
            },
            "targets": [
                {
                    "name": "R1",
                    "ip": "172.16.0.1",
                    "community": "public"
                },
                {
                    "name": "R2",
                    "ip": "172.16.0.2",
                    "community": "public"
                }
            ]
        }

        # Should not raise
        validate_config(cfg)


if __name__ == "__main__":
    unittest.main(verbosity=2)
