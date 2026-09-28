"""The private workspace config resolves from the env var, then data/, then fails."""

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "clickup_workspace_default_path", ROOT / "scripts" / "clickup_workspace.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class ConfigPathTests(unittest.TestCase):
    def test_env_var_wins(self):
        with mock.patch.dict(os.environ, {MODULE.CONFIG_ENV: "/tmp/explicit.json"}):
            self.assertEqual(MODULE.config_path(), Path("/tmp/explicit.json"))

    def test_default_data_file_is_used_when_env_unset(self):
        with tempfile.TemporaryDirectory() as tmp:
            default = Path(tmp) / "clickup-workspace.json"
            default.write_text(json.dumps({"inbox_list_id": "1"}), encoding="utf-8")
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop(MODULE.CONFIG_ENV, None)
                with mock.patch.object(MODULE, "DEFAULT_CONFIG_PATH", default):
                    self.assertEqual(MODULE.config_path(), default)

    def test_missing_both_is_a_config_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "clickup-workspace.json"
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop(MODULE.CONFIG_ENV, None)
                with mock.patch.object(MODULE, "DEFAULT_CONFIG_PATH", missing):
                    with self.assertRaises(MODULE.WorkspaceConfigError):
                        MODULE.config_path()


if __name__ == "__main__":
    unittest.main()
