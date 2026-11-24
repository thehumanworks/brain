import io
import json
import os
import tempfile
import tomllib
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from brain.cli import install, main

BRAIN = "/opt/brain/bin/brain"
COMMAND = f"{BRAIN} brief --hook"
ENTRY = {"matcher": "^(startup|clear)$",
         "hooks": [{"type": "command", "command": COMMAND, "timeout": 5}]}
OTHER = {"hooks": [{"type": "command", "command": "rex session-start"}]}


class Home(unittest.TestCase):
    """A home directory of its own, with `brain` at a made-up path: never your real settings."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name).resolve() / "home"
        self.home.mkdir()
        env = {"HOME": str(self.home), "CLAUDE_CONFIG_DIR": "", "CODEX_HOME": "",
               "PI_CODING_AGENT_DIR": ""}
        for patcher in (
            mock.patch.dict(os.environ, env),
            mock.patch.object(install.service, "watch_command", lambda db: [
                BRAIN, *(["--db", str(db)] if db else []), "watch"]),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.settings = self.home / ".claude" / "settings.json"

    def put(self, path: Path, text: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def brain(self, *argv: str) -> tuple[str, int]:
        """What the command printed, failures after the rest, and its exit status."""
        out, err = io.StringIO(), io.StringIO()
        status = 0
        with redirect_stdout(out), redirect_stderr(err):
            try:
                main(list(argv))
            except SystemExit as e:
                status = e.code
        self.err = err.getvalue()
        return out.getvalue() + self.err, status


class HookTest(Home):
    def test_added_to_a_file_that_is_not_there(self):
        self.assertEqual(install.set_hook(self.settings, COMMAND), "added")
        self.assertEqual(json.loads(self.settings.read_text()), {"hooks": {"SessionStart": [ENTRY]}})
        self.assertTrue(self.settings.read_text().endswith("}\n"))

    def test_everything_else_in_the_file_is_kept_as_it_was(self):
        before = {"model": "opus", "permissions": {"allow": ["Bash(ls:*)"]}, "note": "naïve",
                  "hooks": {"Stop": [OTHER], "SessionStart": [OTHER]}}
        # Four spaces and no newline at the end, as some other tool might write it.
        self.put(self.settings, json.dumps(before, indent=4, ensure_ascii=False))
        self.assertEqual(install.set_hook(self.settings, COMMAND), "added")
        text = self.settings.read_text()
        after = json.loads(text)
        self.assertEqual(after["hooks"]["SessionStart"], [OTHER, ENTRY])
        after["hooks"]["SessionStart"].pop()
        self.assertEqual(after, before)
        self.assertEqual(list(after), list(before))  # the order of the keys, too
        self.assertIn('\n    "model"', text)
        self.assertIn("naïve", text)
        self.assertFalse(text.endswith("\n"))

    def test_twice_writes_nothing_the_second_time(self):
        install.set_hook(self.settings, COMMAND)
        os.utime(self.settings, ns=(1, 1))
        self.assertEqual(install.set_hook(self.settings, COMMAND), "kept")
        self.assertEqual(self.settings.stat().st_mtime_ns, 1)

    def test_the_same_brain_under_another_name_is_kept(self):
        real = self.put(self.home / "tools" / "brain", "")
        (self.home / "bin").mkdir()
        (self.home / "bin" / "brain").symlink_to(real)
        for written in ('"$HOME/bin/brain" brief --hook', "~/bin/brain brief --hook",
                        f"'{real}'  brief  --hook"):
            with self.subTest(written=written):
                entry = {"hooks": [{"type": "command", "command": written}]}
                text = json.dumps({"hooks": {"SessionStart": [entry]}}, indent=2) + "\n"
                self.put(self.settings, text)
                self.assertEqual(install.set_hook(self.settings, f"{real} brief --hook"), "kept")
                self.assertEqual(self.settings.read_text(), text)
                self.assertEqual(install.hook_state(self.settings, f"{real} brief --hook"), "installed")

    def test_another_brain_is_replaced_in_its_own_entry(self):
        old = {"matcher": "startup", "hooks": [
            {"type": "command", "command": "/nix/store/old/bin/brain brief --hook", "timeout": 9}]}
        self.put(self.settings, json.dumps({"hooks": {"SessionStart": [OTHER, old]}}))
        self.assertEqual(install.hook_state(self.settings, COMMAND), "stale")
        self.assertEqual(install.set_hook(self.settings, COMMAND), "updated")
        new = {"matcher": "startup", "hooks": [{"type": "command", "command": COMMAND, "timeout": 9}]}
        self.assertEqual(json.loads(self.settings.read_text()), {"hooks": {"SessionStart": [OTHER, new]}})

    def test_the_index_it_was_given_is_part_of_the_command(self):
        self.assertEqual(install.hook_command(Path("/data/my index.db")),
                         f"{BRAIN} --db '/data/my index.db' brief --hook")
        install.set_hook(self.settings, COMMAND)
        self.assertEqual(install.hook_state(self.settings, install.hook_command(Path("/x.db"))), "stale")

    def test_remove_takes_brains_hook_and_nothing_else(self):
        shared = {"hooks": [OTHER["hooks"][0], ENTRY["hooks"][0]]}
        self.put(self.settings, json.dumps(
            {"model": "opus", "hooks": {"Stop": [OTHER], "SessionStart": [ENTRY, shared, {"hooks": []}]}}))
        self.assertTrue(install.remove_hook(self.settings))
        self.assertEqual(json.loads(self.settings.read_text()), {
            "model": "opus", "hooks": {"Stop": [OTHER], "SessionStart": [OTHER, {"hooks": []}]}})
        os.utime(self.settings, ns=(1, 1))
        self.assertFalse(install.remove_hook(self.settings))
        self.assertEqual(self.settings.stat().st_mtime_ns, 1)

    def test_remove_leaves_no_empty_tables_behind(self):
        self.put(self.settings, json.dumps({"model": "opus"}))
        install.set_hook(self.settings, COMMAND)
        install.remove_hook(self.settings)
        self.assertEqual(json.loads(self.settings.read_text()), {"model": "opus"})
        self.assertFalse(install.remove_hook(self.home / "no" / "such.json"))

    def test_a_file_brain_cannot_read_as_settings_is_left_alone(self):
        for text in ("{ not json", "[1, 2]", '{"hooks": {"SessionStart": {"a": 1}}}', '{"hooks": []}'):
            with self.subTest(text=text):
                self.put(self.settings, text)
                with self.assertRaises(install.CannotInstall):
                    install.set_hook(self.settings, COMMAND)
                self.assertEqual(self.settings.read_text(), text)

    def test_a_link_is_written_through(self):
        real = self.put(self.home / "dotfiles" / "claude.json", '{"model": "opus"}\n')
        self.settings.parent.mkdir()
        self.settings.symlink_to(real)
        install.set_hook(self.settings, COMMAND)
        self.assertTrue(self.settings.is_symlink())
        self.assertEqual(json.loads(real.read_text()), {"model": "opus", "hooks": {"SessionStart": [ENTRY]}})

    @unittest.skipIf(os.geteuid() == 0, "root writes anywhere")
    def test_a_file_that_cannot_be_written_says_what_to_add(self):
        # As when a Nix store path stands behind the settings.
        store = self.home / "store"
        real = self.put(store / "settings.json", '{"model": "opus"}\n')
        store.chmod(0o555)
        self.addCleanup(store.chmod, 0o755)
        self.settings.parent.mkdir()
        self.settings.symlink_to(real)
        with self.assertRaises(install.CannotInstall) as raised:
            install.set_hook(self.settings, COMMAND)
        self.assertIn("~/.claude/settings.json", str(raised.exception))
        self.assertIn(json.dumps(COMMAND), str(raised.exception))
        self.assertEqual(real.read_text(), '{"model": "opus"}\n')
        self.assertEqual([p.name for p in store.iterdir()], ["settings.json"])

    def test_which_hooks_are_brains(self):
        for command in ("brain brief --hook", '"$HOME/.local/bin/brain" brief --hook',
                        "/nix/store/abc-brain-0.1.0/bin/brain --db /a/b.db brief -n 5 --hook"):
            self.assertTrue(install.is_brief_hook(command), command)
        for command in ("brainstorm brief --hook", "echo brain brief --hook", "brain watch",
                        "brain brief", '"brain brief --hook', "", None, 5):
            self.assertFalse(install.is_brief_hook(command), command)


class CodexConfigTest(Home):
    def setUp(self):
        super().setUp()
        self.config = self.home / ".codex" / "config.toml"

    def turn_on(self, before: str) -> str:
        """The config after hooks are turned on in it, checked to differ in that alone."""
        self.put(self.config, before)
        self.assertTrue(install.enable_codex_hooks(self.config))
        after = self.config.read_text()
        expected = tomllib.loads(before)
        expected.setdefault("features", {})["hooks"] = True
        self.assertEqual(tomllib.loads(after), expected)
        self.assertTrue(install.codex_hooks_on(self.config))
        return after

    def test_a_config_that_is_not_there_is_made(self):
        self.assertTrue(install.enable_codex_hooks(self.config))
        self.assertEqual(self.config.read_text(), "[features]\nhooks = true\n")

    def test_added_to_the_features_there_are(self):
        after = self.turn_on('model = "gpt"\n\n[features]  # mine\nmemories = true\n\n[tui]\nhooks = false\n')
        self.assertEqual(after, 'model = "gpt"\n\n[features]  # mine\nhooks = true\nmemories = true\n\n'
                                '[tui]\nhooks = false\n')

    def test_turned_on_where_it_was_off(self):
        after = self.turn_on("[features]\nmemories = true\nhooks = false # not yet\n[tui]\nx = 1\n")
        self.assertEqual(after, "[features]\nmemories = true\nhooks = true\n[tui]\nx = 1\n")

    def test_added_at_the_end_where_there_are_no_features(self):
        for before in ('model = "gpt"\n[tui]\nx = 1', "", "# only a comment\n"):
            with self.subTest(before=before):
                self.assertTrue(self.turn_on(before).endswith("[features]\nhooks = true\n"))

    def test_nothing_is_written_where_hooks_are_on(self):
        self.put(self.config, "features = { hooks = true }\n")
        os.utime(self.config, ns=(1, 1))
        self.assertFalse(install.enable_codex_hooks(self.config))
        self.assertEqual(self.config.stat().st_mtime_ns, 1)

    def test_a_config_brain_does_not_understand_is_left_alone(self):
        for before in ("features = { memories = true }\n", "features.memories = true\n",
                       "features = 1\n", "not toml [\n",
                       # A line that looks like the table but is inside a string.
                       'note = """\n[features]\n"""\n'):
            with self.subTest(before=before):
                self.put(self.config, before)
                with self.assertRaises(install.CannotInstall) as raised:
                    install.enable_codex_hooks(self.config)
                self.assertIn("hooks = true", str(raised.exception))
                self.assertEqual(self.config.read_text(), before)


class PiTest(Home):
    def setUp(self):
        super().setUp()
        self.extension = self.home / ".pi" / "agent" / "extensions" / "brain-brief.ts"
        self.text = install.extension_text(BRAIN)

    def test_the_extension_names_brain_by_its_full_path(self):
        self.assertEqual(install.install_pi(self.text), "added")
        text = self.extension.read_text()
        self.assertIn(f'const BRAIN = process.env.BRAIN_BIN || "{BRAIN}";', text)
        self.assertNotIn('|| "brain"', text)
        self.assertIn("pi.exec(BRAIN, args", text)
        odd = install.extension_text('/a "b"\\c/brain')
        self.assertIn('|| "/a \\"b\\"\\\\c/brain";', odd)

    def test_twice_writes_nothing_the_second_time(self):
        install.install_pi(self.text)
        os.utime(self.extension, ns=(1, 1))
        self.assertEqual(install.install_pi(self.text), "kept")
        self.assertEqual(self.extension.stat().st_mtime_ns, 1)
        self.assertEqual(install.pi_state(self.text), "installed")

    def test_a_link_from_a_setup_by_hand_becomes_the_file(self):
        checkout = self.put(self.home / "brain" / "integrations" / "pi" / "brain-brief.ts", "// old\n")
        self.extension.parent.mkdir(parents=True)
        self.extension.symlink_to(checkout)
        self.assertEqual(install.pi_state(self.text), "stale")
        self.assertEqual(install.install_pi(self.text), "updated")
        self.assertFalse(self.extension.is_symlink())
        self.assertEqual(self.extension.read_text(), self.text)
        self.assertEqual(checkout.read_text(), "// old\n")

    def test_an_extension_from_another_brain_is_updated(self):
        install.install_pi(install.extension_text("/old/brain"))
        self.assertEqual(install.pi_state(self.text), "stale")
        self.assertEqual(install.install_pi(self.text), "updated")
        self.assertEqual(self.extension.read_text(), self.text)

    def test_remove(self):
        self.assertFalse(install.uninstall_pi())
        install.install_pi(self.text)
        other = self.put(self.extension.with_name("other.ts"), "")
        self.assertTrue(install.uninstall_pi())
        self.assertEqual(list(self.extension.parent.iterdir()), [other])
        self.extension.symlink_to(self.home / "gone")  # a link whose file was deleted
        self.assertTrue(install.uninstall_pi())
        self.assertFalse(self.extension.is_symlink())


class CommandTest(Home):
    def setUp(self):
        super().setUp()
        self.service = {"path": None}

        def service_install(db):
            self.service["path"] = self.home / "Library" / "LaunchAgents" / "local.brain.plist"
            print(f"installed {self.service['path']}\nbrain watch now runs in the background")

        def service_uninstall():
            self.service["path"] = None
            print("removed the brain launchd agent")

        for name, fake in (("install", service_install), ("uninstall", service_uninstall),
                           ("installed_path", lambda: self.service["path"])):
            patcher = mock.patch.object(install.service, name, fake)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_everything_at_once_leaves_out_agents_that_are_not_here(self):
        (self.home / ".claude").mkdir()
        out, status = self.brain("install")
        self.assertEqual(status, 0)
        self.assertEqual(out.splitlines(), [
            "service  installed ~/Library/LaunchAgents/local.brain.plist",
            "         brain watch now runs in the background",
            "claude   added the SessionStart hook in ~/.claude/settings.json",
            "codex    skipped: ~/.codex does not exist (brain install --codex sets it up anyway)",
            "pi       skipped: ~/.pi/agent does not exist (brain install --pi sets it up anyway)",
        ])
        self.assertFalse((self.home / ".codex").exists() or (self.home / ".pi").exists())

    def test_everything_at_once_then_check_then_remove(self):
        for directory in (".claude", ".codex", ".pi/agent"):
            (self.home / directory).mkdir(parents=True)
        out, status = self.brain("install", "--check")
        self.assertEqual(status, 1)
        self.assertEqual(out.splitlines(), [
            "service  not installed",
            "claude   not installed: no SessionStart hook in ~/.claude/settings.json",
            "codex    not installed: no SessionStart hook in ~/.codex/hooks.json",
            "pi       not installed: no ~/.pi/agent/extensions/brain-brief.ts",
            "",
            "brain install sets up what is missing",
        ])
        self.assertFalse(self.settings.exists())  # checking changes nothing

        out, status = self.brain("install")
        self.assertEqual(status, 0)
        self.assertEqual(out.splitlines()[2:], [
            "claude   added the SessionStart hook in ~/.claude/settings.json",
            "codex    added the SessionStart hook in ~/.codex/hooks.json",
            "         turned hooks on in ~/.codex/config.toml",
            "         Codex runs a new hook only once you trust it: review it with /hooks in Codex",
            "pi       wrote ~/.pi/agent/extensions/brain-brief.ts",
        ])
        hooks = {"hooks": {"SessionStart": [ENTRY]}}
        self.assertEqual(json.loads(self.settings.read_text()), hooks)
        self.assertEqual(json.loads((self.home / ".codex" / "hooks.json").read_text()), hooks)

        out, status = self.brain("install", "--check")
        self.assertEqual(status, 0)
        self.assertEqual(out.splitlines(), [
            f"service  installed: ~/Library/LaunchAgents/local.brain.plist",
            "claude   installed: SessionStart hook in ~/.claude/settings.json",
            "codex    installed: SessionStart hook in ~/.codex/hooks.json",
            "pi       installed: ~/.pi/agent/extensions/brain-brief.ts",
        ])

        out, status = self.brain("install", "--claude", "--codex", "--pi")
        self.assertEqual(status, 0)
        self.assertEqual(out.splitlines(), [
            "claude   already has the SessionStart hook in ~/.claude/settings.json",
            "codex    already has the SessionStart hook in ~/.codex/hooks.json",
            "pi       already has ~/.pi/agent/extensions/brain-brief.ts",
        ])

        out, status = self.brain("uninstall")
        self.assertEqual(status, 0)
        self.assertEqual(out.splitlines(), [
            "service  removed the brain launchd agent",
            "claude   removed the SessionStart hook from ~/.claude/settings.json",
            "codex    removed the SessionStart hook from ~/.codex/hooks.json",
            "pi       removed ~/.pi/agent/extensions/brain-brief.ts",
        ])
        self.assertEqual(json.loads(self.settings.read_text()), {})
        out, status = self.brain("uninstall", "--claude", "--pi")
        self.assertEqual(out.splitlines(), ["claude   was not installed", "pi       was not installed"])
        self.assertEqual(self.brain("install", "--check")[1], 1)

    def test_a_named_agent_is_set_up_where_it_has_no_directory(self):
        out, status = self.brain("install", "--codex", "--pi")
        self.assertEqual(status, 0)
        self.assertTrue((self.home / ".codex" / "hooks.json").exists())
        self.assertTrue((self.home / ".pi" / "agent" / "extensions" / "brain-brief.ts").exists())
        self.assertNotIn("service", out)
        self.assertIsNone(self.service["path"])

    def test_one_part_failing_does_not_stop_the_others(self):
        self.put(self.settings, "{ not json")
        out, status = self.brain("install", "--claude", "--pi")
        self.assertEqual(status, 1)
        self.assertTrue(self.err.startswith("claude   failed: ~/.claude/settings.json is not valid JSON"))
        self.assertEqual(out.splitlines()[0], "pi       wrote ~/.pi/agent/extensions/brain-brief.ts")
        self.assertEqual(self.settings.read_text(), "{ not json")

    def test_a_service_that_cannot_run_here_is_said_and_the_rest_is_set_up(self):
        def no_service(db):
            raise SystemExit("no systemd user manager is running here; run `brain watch` instead")
        (self.home / ".claude").mkdir()
        with mock.patch.object(install.service, "install", no_service):
            out, status = self.brain("install")
        self.assertEqual(status, 1)
        self.assertEqual(self.err, "service  failed: no systemd user manager is running here; run "
                                   "`brain watch` instead\n")
        self.assertEqual(out.splitlines()[0], "claude   added the SessionStart hook in ~/.claude/settings.json")

    def test_quietly_only_what_changed_or_failed_is_said(self):
        (self.home / ".claude").mkdir()
        out, status = self.brain("install", "--quiet", "--claude", "--pi")
        self.assertEqual(out.splitlines(), ["claude   added the SessionStart hook in ~/.claude/settings.json",
                                            "pi       wrote ~/.pi/agent/extensions/brain-brief.ts"])
        self.assertEqual(self.brain("install", "--quiet", "--claude", "--pi"), ("", 0))
        self.settings.write_text("[]")
        out, status = self.brain("install", "--quiet", "--claude", "--pi")
        self.assertEqual((out, status), (self.err, 1))
        self.assertTrue(out.startswith("claude   failed: "))

    def test_codex_with_a_config_brain_cannot_change_still_gets_the_hook(self):
        self.put(self.home / ".codex" / "config.toml", "features = { memories = true }\n")
        out, status = self.brain("install", "--codex")
        self.assertEqual(status, 0)
        self.assertIn("add `hooks = true` under `[features]` in ~/.codex/config.toml", out)
        out, status = self.brain("install", "--check", "--codex")
        self.assertEqual(status, 1)
        self.assertEqual(out.splitlines()[0], "codex    SessionStart hook in ~/.codex/hooks.json, "
                                              "but hooks are off in ~/.codex/config.toml")

    def test_the_index_and_the_agents_own_directories_are_followed(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.home / "cc"),
                                          "CODEX_HOME": str(self.home / "cx"),
                                          "PI_CODING_AGENT_DIR": str(self.home / "p")}):
            out, status = self.brain("--db", "/data/index.db", "install", "--claude", "--codex", "--pi")
        self.assertEqual(status, 0)
        command = f"{BRAIN} --db /data/index.db brief --hook"
        for path in (self.home / "cc" / "settings.json", self.home / "cx" / "hooks.json"):
            self.assertEqual(json.loads(path.read_text())["hooks"]["SessionStart"][0]["hooks"][0]["command"],
                             command)
        self.assertTrue((self.home / "p" / "extensions" / "brain-brief.ts").exists())
        self.assertIn("~/cc/settings.json", out)


if __name__ == "__main__":
    unittest.main()
