import unittest

# Import functions from poller.py
# This way the test checks the real code
from poller import load_config, validate_config


class TestConfig(unittest.TestCase):

	# TEST 1: The real config.yml loads and passes validation
    def test_config_loads_and_validates(self):
	# A = Load config.ymal into the Python dictionary
        cfg = load_config("config.yml")

	# B = Basic checks (struckture)
        self.assertIsInstance(cfg, dict)
        self.assertIn("defaults", cfg)
        self.assertIn("targets", cfg)

        # should not raise an error
	# If it raises, the test fails
        validate_config(cfg)


        # extra checks for lab requirements 2 targets
        self.assertGreaterEqual(len(cfg["targets"]), 2)

	# defaults.oids should be a non-empty list
        self.assertIsInstance(cfg["defaults"]["oids"], list)
        self.assertGreaterEqual(len(cfg["defaults"]["oids"]), 1)


	# TEST 2: A broken config should fail validation
    def test_invalid_config_missing_keys(self):

	# This config is missing 'defaults' and 'targets'
        bad = {"hello": "world"}

	# validate_config() should raise ValueError, If not the test fails
        with self.assertRaises(ValueError):
            validate_config(bad)

# Standard unittest entry point
if __name__ == "__main__":
    # verbosity=2 prints more details when run the tests
    unittest.main(verbosity=2)
