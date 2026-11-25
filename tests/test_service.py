import os
import plistlib
import shlex
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from brain.cli import service

COMMAND = ["/home/a b/.local/bin/brain", "--db", "/data/100%/$x/index.db", "watch"]
ENV = {"CODEX_HOME": '/home/a b/co"dex', "BRAIN_DB": "/data/100%/index.db"}


class ServiceTest(unittest.TestCase):
    def test_launchd_plist(self):
        plist = plistlib.loads(service.launchd_plist(COMMAND, ENV))
        self.assertEqual(plist["Label"], "local.brain")
        self.assertEqual(plist["ProgramArguments"], COMMAND)
        self.assertEqual(plist["EnvironmentVariables"], ENV)
        self.assertTrue(plist["RunAtLoad"] and plist["KeepAlive"])
        self.assertNotIn("EnvironmentVariables", plistlib.loads(service.launchd_plist(COMMAND, {})))

    def test_systemd_unit(self):
        unit = service.systemd_unit(COMMAND, ENV)
        lines = dict(line.split("=", 1) for line in unit.splitlines() if line.startswith("ExecStart="))
        # Unescape as systemd does: %% and $$ to % and $, then shell-like quoting.
        exec_start = lines["ExecStart"].replace("%%", "%").replace("$$", "$")
        self.assertEqual(shlex.split(exec_start), COMMAND)
        envs = [line.split("=", 1)[1] for line in unit.splitlines() if line.startswith("Environment=")]
        self.assertEqual(
            [shlex.split(e.replace("%%", "%"))[0] for e in envs],
            [f"{k}={v}" for k, v in ENV.items()],
        )
        self.assertIn("WantedBy=default.target", unit)

    def test_brain_is_named_by_a_path_that_updates_keep(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            store, profile, other = tmp / "store/brain-1/bin", tmp / "profile/bin", tmp / "other/bin"
            for directory in (store, profile, other):
                directory.mkdir(parents=True)
            for directory in (store, other):
                (directory / "brain").write_text("#!/bin/sh\n")
                (directory / "brain").chmod(0o755)
            # A Home Manager profile links to the package in the store.
            (profile / "brain").symlink_to(store / "brain")

            def named(*path: Path) -> str:
                with mock.patch.dict(os.environ, {"PATH": os.pathsep.join(map(str, path))}), \
                        mock.patch.object(service, "NIX_STORE", f"{tmp / 'store'}/"):
                    return service.own_path()

            # Nix's wrapper puts its own directory first; another `brain` is passed over.
            self.assertEqual(named(store, other, profile), str(profile / "brain"))
            self.assertEqual(named(store), str(store / "brain"))
            self.assertEqual(named(profile, store), str(profile / "brain"))
            # Outside Nix, the first on PATH, as before.
            self.assertEqual(named(other, store, profile), str(other / "brain"))
            with self.assertRaises(SystemExit):
                named(tmp)


if __name__ == "__main__":
    unittest.main()
