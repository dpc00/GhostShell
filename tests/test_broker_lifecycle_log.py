"""The broker's lifecycle log must be off by default (AGENTS.md rule 14).

Without the developer switch the broker creates no folder, opens no file and
leaves sys.stdout / sys.stderr alone. With the switch it logs as before.
"""
import importlib.util
import os
import sys
import tempfile
import unittest

_BROKER_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "agent_broker.py"
)


def _load_broker():
    spec = importlib.util.spec_from_file_location("agent_broker_under_test", _BROKER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LifecycleLogSwitchTests(unittest.TestCase):
    def setUp(self):
        self.broker = _load_broker()
        self.saved_streams = (sys.stdout, sys.stderr)
        self.saved_switch = os.environ.pop(self.broker.LIFECYCLE_LOG_SWITCH, None)
        self.tmp = tempfile.TemporaryDirectory()
        self.log_path = os.path.join(self.tmp.name, "logs", "agent_broker.log")

    def tearDown(self):
        stream = sys.stdout
        sys.stdout, sys.stderr = self.saved_streams
        if stream not in self.saved_streams and hasattr(stream, "close"):
            stream.close()
        os.environ.pop(self.broker.LIFECYCLE_LOG_SWITCH, None)
        if self.saved_switch is not None:
            os.environ[self.broker.LIFECYCLE_LOG_SWITCH] = self.saved_switch
        self.tmp.cleanup()

    def test_off_by_default_creates_nothing(self):
        self.broker._configure_lifecycle_log(self.log_path)
        self.assertFalse(os.path.exists(os.path.dirname(self.log_path)))
        self.assertEqual((sys.stdout, sys.stderr), self.saved_streams)

    def test_switch_set_to_something_else_is_still_off(self):
        os.environ[self.broker.LIFECYCLE_LOG_SWITCH] = "0"
        self.broker._configure_lifecycle_log(self.log_path)
        self.assertFalse(os.path.exists(self.log_path))

    def test_switch_on_logs(self):
        os.environ[self.broker.LIFECYCLE_LOG_SWITCH] = "1"
        self.broker._configure_lifecycle_log(self.log_path)
        print("lifecycle line")
        sys.stdout.flush()
        self.assertTrue(os.path.exists(self.log_path))
        with open(self.log_path, encoding="utf-8") as handle:
            self.assertIn("lifecycle line", handle.read())

    def test_no_path_is_a_no_op_even_with_switch(self):
        os.environ[self.broker.LIFECYCLE_LOG_SWITCH] = "1"
        self.broker._configure_lifecycle_log(None)
        self.assertEqual((sys.stdout, sys.stderr), self.saved_streams)


if __name__ == "__main__":
    unittest.main()
