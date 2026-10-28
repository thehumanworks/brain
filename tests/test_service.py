import plistlib
import shlex
import unittest

from brain import service

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


if __name__ == "__main__":
    unittest.main()
