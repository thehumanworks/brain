"""Set `brain` up on this machine in one step: the background watcher (`brain.cli.service`), the
SessionStart hook that starts each Claude Code and Codex session with `brain brief`, and the Pi
extension that does the same.

Each part can be installed, checked and removed alone. Installing twice changes nothing, and
whatever else an agent's settings hold is left as it is. A settings file that cannot be written,
such as one a Nix store path stands behind, is reported with what to add to it by hand."""

import contextlib
import io
import json
import os
import re
import shlex
import shutil
import sys
import tomllib
from importlib import resources
from pathlib import Path

from brain.cli import service

PARTS = ("service", "claude", "codex", "pi")
# Only sessions someone starts: `brain brief --hook` also leaves out resumed and compacted ones.
MATCHER = "^(startup|clear)$"
EXTENSION = "brain-brief.ts"
# The line of the Pi extension that names the command, and what `brain install` puts for the name.
EXTENSION_NAME = '|| "brain";'


class CannotInstall(Exception):
    """A part that could not be set up or removed, with what to do about it."""


# --- Where each agent keeps its settings ---------------------------------------------------------


def claude_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def codex_dir() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def pi_dir() -> Path:
    return Path(os.environ.get("PI_CODING_AGENT_DIR") or Path.home() / ".pi" / "agent")


def claude_settings() -> Path:
    return claude_dir() / "settings.json"


def codex_hooks() -> Path:
    return codex_dir() / "hooks.json"


def codex_config() -> Path:
    return codex_dir() / "config.toml"


def pi_extension() -> Path:
    return pi_dir() / "extensions" / EXTENSION


def tilde(path: Path) -> str:
    home = str(Path.home())
    text = str(path)
    return "~" + text[len(home):] if text == home or text.startswith(home + "/") else text


# --- The hook, in a settings file of Claude Code's shape, which Codex shares ----------------------


def brain_command(db: Path | None) -> list[str]:
    """`brain` by its full path, with the index it was given: what the service runs, too."""
    return service.watch_command(db)[:-1]


def hook_command(db: Path | None) -> str:
    return shlex.join([*brain_command(db), "brief", "--hook"])


def hook_entry(command: str) -> dict:
    return {"matcher": MATCHER, "hooks": [{"type": "command", "command": command, "timeout": 5}]}


def hook_snippet(command: str) -> str:
    return json.dumps({"hooks": {"SessionStart": [hook_entry(command)]}}, indent=2)


def words(command: object) -> list[str] | None:
    """A hook's command as the words a shell would run, or None if it is not one command."""
    if not isinstance(command, str):
        return None
    try:
        return shlex.split(command)
    except ValueError:
        return None


def is_brief_hook(command: object) -> bool:
    """Whether a hook runs `brain brief --hook`, however `brain` is named in it."""
    argv = words(command)
    return bool(argv) and Path(argv[0]).name == "brain" and "brief" in argv and "--hook" in argv


def resolved(argv: list[str]) -> list[str]:
    """The words with the program as the file it is, so `"$HOME/.local/bin/brain"` and the full
    path of the same file compare equal."""
    program = os.path.expanduser(os.path.expandvars(argv[0]))
    if os.sep not in program:
        program = shutil.which(program) or program
    return [os.path.realpath(program), *argv[1:]]


def same_command(a: str, b: str) -> bool:
    return resolved(words(a) or [a]) == resolved(words(b) or [b])


def read_settings(path: Path) -> tuple[dict, str]:
    """A settings file's contents and its text; an empty one where there is no file."""
    try:
        text = path.read_text()
    except FileNotFoundError:
        return {}, ""
    except OSError as e:
        raise CannotInstall(f"cannot read {tilde(path)}: {e.strerror}") from e
    try:
        data = json.loads(text) if text.strip() else {}
    except json.JSONDecodeError as e:
        raise CannotInstall(f"{tilde(path)} is not valid JSON ({e}); it is left as it is") from e
    if not isinstance(data, dict):
        raise CannotInstall(f"{tilde(path)} does not hold a JSON object; it is left as it is")
    return data, text


def brief_hooks(data: dict) -> list[dict]:
    """The hooks in a settings file that run `brain brief --hook`."""
    hooks = data.get("hooks")
    entries = hooks.get("SessionStart") if isinstance(hooks, dict) else None
    return [hook for entry in entries or [] if isinstance(entry, dict)
            for hook in entry.get("hooks") or []
            if isinstance(hook, dict) and is_brief_hook(hook.get("command"))]


def write_settings(path: Path, data: dict, before: str, command: str | None = None) -> None:
    """Write a settings file in the indentation it had, through a link if it is one, as dotfile
    managers keep them."""
    indent = re.search(r'\n([ \t]+)"', before)
    text = json.dumps(data, indent=indent.group(1) if indent else 2, ensure_ascii=False)
    write_file(path, text + ("\n" if not before or before.endswith("\n") else ""),
               f", add this to it:\n{hook_snippet(command)}" if command else "")


def write_file(path: Path, text: str, by_hand: str = "") -> None:
    target = path.resolve() if path.is_symlink() else path
    temporary = target.with_name(target.name + ".brain-new")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(text)
        if target.exists():
            shutil.copymode(target, temporary)
        os.replace(temporary, target)
    except OSError as e:
        with contextlib.suppress(OSError):
            temporary.unlink(missing_ok=True)
        raise CannotInstall(f"cannot write {tilde(path)} ({e.strerror}). If another tool keeps "
                            f"that file{by_hand or ', change it there'}") from e


def set_hook(path: Path, command: str) -> str:
    """Make a settings file run `command` at the start of a session: 'added', 'updated' when it
    ran another `brain`, or 'kept' when it ran this one already and nothing was written."""
    data, before = read_settings(path)
    found = brief_hooks(data)
    if found and all(same_command(hook["command"], command) for hook in found):
        return "kept"
    for hook in found:
        hook["command"] = command
    if not found:
        hooks = data.setdefault("hooks", {})
        entries = hooks.setdefault("SessionStart", []) if isinstance(hooks, dict) else None
        if not isinstance(entries, list):
            raise CannotInstall(f"the hooks in {tilde(path)} are not in the shape brain knows; "
                                f"add this to it:\n{hook_snippet(command)}")
        entries.append(hook_entry(command))
    write_settings(path, data, before, command)
    return "updated" if found else "added"


def remove_hook(path: Path) -> bool:
    """Take `brain brief --hook` out of a settings file, and nothing else: whether it was there."""
    data, before = read_settings(path)
    found = brief_hooks(data)
    if not found:
        return False
    hooks = data["hooks"]
    entries = []
    for entry in hooks["SessionStart"]:
        ours = [hook for hook in entry.get("hooks") or [] if any(hook is f for f in found)] \
            if isinstance(entry, dict) else []
        if ours:
            entry["hooks"] = [hook for hook in entry["hooks"] if not any(hook is f for f in ours)]
        # An entry that held nothing but brain's hook goes with it.
        if not ours or entry["hooks"]:
            entries.append(entry)
    hooks["SessionStart"] = entries
    if not hooks["SessionStart"]:
        del hooks["SessionStart"]
    if not hooks:
        del data["hooks"]
    write_settings(path, data, before)
    return True


def hook_state(path: Path, command: str) -> str:
    """'installed', 'stale' when the hook runs another `brain`, or 'missing'."""
    found = brief_hooks(read_settings(path)[0])
    if not found:
        return "missing"
    return "installed" if all(same_command(h["command"], command) for h in found) else "stale"


# --- Codex: hooks run only with the feature turned on ---------------------------------------------


def codex_hooks_on(path: Path) -> bool:
    try:
        features = tomllib.loads(path.read_text()).get("features")
    except (OSError, tomllib.TOMLDecodeError):
        return False
    return isinstance(features, dict) and features.get("hooks") is True


def with_codex_hooks(text: str) -> str:
    """Codex's config.toml with `hooks = true` under `[features]`, and nothing else changed."""
    lines = text.splitlines()
    header = next((i for i, line in enumerate(lines)
                   if re.fullmatch(r"\s*\[features\]\s*(#.*)?", line)), None)
    if header is None:
        return text + ("" if not text or text.endswith("\n") else "\n") + \
            ("\n" if text.strip() else "") + "[features]\nhooks = true\n"
    end = next((i for i in range(header + 1, len(lines)) if lines[i].lstrip().startswith("[")),
               len(lines))
    setting = next((i for i in range(header + 1, end)
                    if re.match(r"\s*hooks\s*=", lines[i])), None)
    if setting is None:
        lines.insert(header + 1, "hooks = true")
    else:
        lines[setting] = "hooks = true"
    return "\n".join(lines) + "\n"


def enable_codex_hooks(path: Path) -> bool:
    """Turn Codex's hooks on in its config.toml: whether the file had to change. The result is
    read back before it is written, so a config this does not understand is never damaged."""
    if codex_hooks_on(path):
        return False
    by_hand = f"add `hooks = true` under `[features]` in {tilde(path)} for Codex to run hooks"
    try:
        before = path.read_text() if path.exists() else ""
        expected = tomllib.loads(before)
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise CannotInstall(f"cannot read {tilde(path)} ({e}); {by_hand}") from e
    if not isinstance(expected.setdefault("features", {}), dict):
        raise CannotInstall(by_hand)
    expected["features"]["hooks"] = True
    after = with_codex_hooks(before)
    try:
        if tomllib.loads(after) != expected:
            raise CannotInstall(by_hand)
    except tomllib.TOMLDecodeError as e:
        raise CannotInstall(by_hand) from e
    try:
        write_file(path, after)
    except CannotInstall as e:
        raise CannotInstall(f"{e}; {by_hand}") from e
    return True


# --- Pi: an extension, copied with the full path of `brain` in it ---------------------------------


def extension_text(brain: str) -> str:
    source = resources.files("brain").joinpath("integrations", "pi", EXTENSION).read_text()
    if source.count(EXTENSION_NAME) != 1:
        raise CannotInstall("brain's Pi extension has no line naming the command; this is a bug")
    return source.replace(EXTENSION_NAME, f"|| {json.dumps(brain)};")


def pi_state(text: str) -> str:
    path = pi_extension()
    if not path.is_symlink() and not path.exists():
        return "missing"
    try:
        return "installed" if not path.is_symlink() and path.read_text() == text else "stale"
    except OSError:
        return "stale"


def install_pi(text: str) -> str:
    """'added', 'updated', or 'kept' when the extension is there as this `brain` writes it. A
    link from an earlier setup by hand is replaced by the file."""
    state = pi_state(text)
    if state == "installed":
        return "kept"
    path = pi_extension()
    if path.is_symlink():
        path.unlink()
    write_file(path, text)
    return "added" if state == "missing" else "updated"


def uninstall_pi() -> bool:
    path = pi_extension()
    if not path.is_symlink() and not path.exists():
        return False
    try:
        path.unlink()
    except OSError as e:
        raise CannotInstall(f"cannot remove {tilde(path)}: {e.strerror}") from e
    return True


# --- The commands ---------------------------------------------------------------------------------


def chosen(args) -> tuple[list[str], bool]:
    """The parts asked for, and whether they were named: with none named, all of them."""
    named = [part for part in PARTS if getattr(args, part)]
    return named or list(PARTS), bool(named)


def absent(part: str) -> Path | None:
    """The directory of an agent that is not used here, so its part is left out unless named."""
    directory = {"claude": claude_dir, "codex": codex_dir, "pi": pi_dir}.get(part)
    return directory() if directory and not directory().is_dir() else None


def say(part: str, text: str, failed: bool = False) -> None:
    first, *rest = text.splitlines() or [""]
    out = sys.stderr if failed else sys.stdout
    print(f"{part:8} {first}", file=out)
    for line in rest:
        print(f"{'':8} {line}", file=out)


def quiet(action, *args) -> str:
    """What `brain service` printed, to say under the part's name."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        action(*args)
    return out.getvalue().strip().replace(str(Path.home()) + "/", "~/")


VERB = {"added": "added", "updated": "updated", "kept": "already has"}


def install_part(part: str, db: Path | None) -> tuple[bool, str]:
    """Whether anything changed, and what to say."""
    if part == "service":
        return True, quiet(service.install, db)
    if part == "pi":
        change = install_pi(extension_text(brain_command(None)[0]))
        verb = {"added": "wrote", "updated": "updated", "kept": "already has"}[change]
        return change != "kept", f"{verb} {tilde(pi_extension())}"
    command = hook_command(db)
    if part == "claude":
        change = set_hook(claude_settings(), command)
        return change != "kept", f"{VERB[change]} the SessionStart hook in {tilde(claude_settings())}"
    change = set_hook(codex_hooks(), command)
    changed = change != "kept"
    text = f"{VERB[change]} the SessionStart hook in {tilde(codex_hooks())}"
    try:
        if enable_codex_hooks(codex_config()):
            changed = True
            text += f"\nturned hooks on in {tilde(codex_config())}"
    except CannotInstall as e:
        changed = True
        text += f"\n{e}"
    if change != "kept":
        text += "\nCodex runs a new hook only once you trust it: review it with /hooks in Codex"
    return changed, text


def uninstall_part(part: str) -> tuple[bool, str]:
    if part == "service":
        return True, quiet(service.uninstall)
    if part == "pi":
        removed, text = uninstall_pi(), f"removed {tilde(pi_extension())}"
    else:
        path = claude_settings() if part == "claude" else codex_hooks()
        removed, text = remove_hook(path), f"removed the SessionStart hook from {tilde(path)}"
    return removed, text if removed else "was not installed"


def check_part(part: str, db: Path | None) -> tuple[bool, str]:
    if part == "service":
        path = service.installed_path()
        return (True, f"installed: {tilde(path)}") if path else (False, "not installed")
    if part == "pi":
        state = pi_state(extension_text(brain_command(None)[0]))
        where = tilde(pi_extension())
    else:
        path = claude_settings() if part == "claude" else codex_hooks()
        state = hook_state(path, hook_command(db))
        where = f"SessionStart hook in {tilde(path)}"
        if part == "codex" and state == "installed" and not codex_hooks_on(codex_config()):
            return False, f"{where}, but hooks are off in {tilde(codex_config())}"
    return {"installed": (True, f"installed: {where}"),
            "stale": (False, f"out of date: {where} is not as this brain writes it"),
            "missing": (False, f"not installed: no {where}")}[state]


def run(args, action: str) -> None:
    """`brain install`, `brain install --check` and `brain uninstall`: each part on a line of its
    own, and exit status 1 if one of them could not be done or, checking, is not set up. With
    `--quiet`, only what changed and what failed is said, as suits a script that runs it often."""
    parts, named = chosen(args)
    quietly = getattr(args, "quiet", False)
    failed = False
    for part in parts:
        missing = None if named else absent(part)
        if missing and action != "uninstall":
            if not quietly:
                say(part, f"skipped: {tilde(missing)} does not exist (brain install --{part} "
                          "sets it up anyway)")
            continue
        try:
            if action == "check":
                ok, text = check_part(part, args.db)
                failed |= not ok
                changed = True
            elif action == "install":
                changed, text = install_part(part, args.db)
            else:
                changed, text = uninstall_part(part)
        except (CannotInstall, SystemExit) as e:
            failed = True
            say(part, f"failed: {e}", failed=True)
            continue
        if changed or not quietly:
            say(part, text)
    if failed:
        if action == "check":
            print("\nbrain install sets up what is missing")
        sys.exit(1)
