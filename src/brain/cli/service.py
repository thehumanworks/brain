"""Run `brain watch` in the background from login on: a launchd agent on macOS, a systemd user
service on Linux and WSL. Both run as you, restart it if it stops, and keep it at low priority."""

import os
import platform
import plistlib
import shutil
import subprocess
import time
from pathlib import Path

LABEL = "local.brain"
UNIT = "brain.service"
# Services start with an almost empty environment, so the variables that move the agents'
# directories or the index are carried over from the shell that installs the service.
PASSED_ENV = ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "PI_CODING_AGENT_DIR", "BRAIN_DB", "XDG_DATA_HOME")
# Seconds between restarts, so a watcher failing on start doesn't spin.
RESTART_DELAY = 30


def watch_command(db: Path | None) -> list[str]:
    exe = shutil.which("brain")
    if not exe:
        raise SystemExit("`brain` is not on PATH; install it first: uv tool install <path to brain>")
    return [os.path.abspath(exe), *(["--db", str(db.resolve())] if db else []), "watch"]


def passed_env() -> dict[str, str]:
    return {k: os.environ[k] for k in PASSED_ENV if os.environ.get(k)}


def run(*cmd: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if check and proc.returncode:
        raise SystemExit(f"`{' '.join(cmd)}` failed: {(proc.stderr or proc.stdout).strip()}")
    return proc


def install(db: Path | None) -> None:
    {"Darwin": launchd_install, "Linux": systemd_install}.get(platform.system(), unsupported)(db)


def uninstall() -> None:
    {"Darwin": launchd_uninstall, "Linux": systemd_uninstall}.get(platform.system(), unsupported)()


def status() -> None:
    {"Darwin": launchd_status, "Linux": systemd_status}.get(platform.system(), unsupported)()


def unsupported(*_) -> None:
    raise SystemExit(f"no background service for {platform.system()}; run `brain watch` instead")


# --- macOS: launchd ---------------------------------------------------------------------------


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def log_path() -> Path:
    return Path.home() / "Library" / "Logs" / "brain.log"


def launchd_plist(command: list[str], env: dict[str, str]) -> bytes:
    plist = {
        "Label": LABEL,
        "ProgramArguments": command,
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": RESTART_DELAY,
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "Nice": 10,
        "StandardOutPath": str(log_path()),
        "StandardErrorPath": str(log_path()),
    }
    if env:
        plist["EnvironmentVariables"] = env
    return plistlib.dumps(plist)


def domain() -> str:
    return f"gui/{os.getuid()}"


def launchd_loaded() -> bool:
    return run("launchctl", "print", f"{domain()}/{LABEL}", check=False).returncode == 0


def launchd_unload() -> None:
    run("launchctl", "bootout", f"{domain()}/{LABEL}", check=False)
    # bootout returns before the job is gone, and bootstrapping it again meanwhile fails.
    for _ in range(50):
        if not launchd_loaded():
            return
        time.sleep(0.1)


def launchd_install(db: Path | None) -> None:
    path = plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    log_path().parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(launchd_plist(watch_command(db), passed_env()))
    launchd_unload()
    run("launchctl", "bootstrap", domain(), str(path))
    print(f"installed {path}\nbrain watch now runs in the background and at every login")
    print(f"log: {log_path()}")


def launchd_uninstall() -> None:
    launchd_unload()
    plist_path().unlink(missing_ok=True)
    print("removed the brain launchd agent")


def launchd_status() -> None:
    proc = run("launchctl", "print", f"{domain()}/{LABEL}", check=False)
    if proc.returncode:
        print("not installed (brain service install)" if not plist_path().exists()
              else f"installed but not loaded: {plist_path()}")
        return
    # The job's own fields are indented once; deeper ones belong to its sub-sections.
    fields = dict(
        line.strip().split(" = ", 1) for line in proc.stdout.splitlines()
        if line.startswith("\t") and not line.startswith("\t\t") and " = " in line
    )
    parts = [fields.get("state", "?") + (f" (pid {fields['pid']})" if "pid" in fields else ""),
             f"{fields.get('runs', '?')} starts since loaded"]
    if last := fields.get("last terminating signal") or fields.get("last exit code"):
        parts.append(f"last stopped: {last}")
    print(" · ".join(parts))
    print_tail(log_path())


def print_tail(path: Path, lines: int = 5) -> None:
    try:
        tail = path.read_text(errors="replace").splitlines()[-lines:]
    except FileNotFoundError:
        return
    print(f"\n{path}:")
    print("\n".join(tail))


# --- Linux: systemd ---------------------------------------------------------------------------


def unit_path() -> Path:
    config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return config / "systemd" / "user" / UNIT


def unit_quote(value: str, command: bool = False) -> str:
    """A double-quoted unit file value: `%` starts a specifier, and in commands `$` a variable."""
    value = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return '"' + (value.replace("$", "$$") if command else value) + '"'


def systemd_unit(command: list[str], env: dict[str, str]) -> str:
    lines = [
        "[Unit]",
        "Description=brain: index Claude Code, Codex and Pi conversation history",
        "",
        "[Service]",
        "ExecStart=" + " ".join(unit_quote(a, command=True) for a in command),
        *(f"Environment={unit_quote(f'{k}={v}')}" for k, v in env.items()),
        "Restart=always",
        f"RestartSec={RESTART_DELAY}",
        "Nice=10",
        "IOSchedulingClass=idle",
        "",
        "[Install]",
        "WantedBy=default.target",
    ]
    return "\n".join(lines) + "\n"


def systemctl(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return run("systemctl", "--user", *args, check=check)


def require_systemd() -> None:
    if shutil.which("systemctl") and systemctl("show-environment", check=False).returncode == 0:
        return
    if "microsoft" in platform.release().lower():
        raise SystemExit(
            "systemd is not running in this WSL distribution. Add these lines to /etc/wsl.conf,\n"
            "run `wsl --shutdown` from Windows and open the distribution again:\n\n"
            "[boot]\nsystemd=true"
        )
    raise SystemExit("no systemd user manager is running here; run `brain watch` instead")


def systemd_install(db: Path | None) -> None:
    require_systemd()
    path = unit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(systemd_unit(watch_command(db), passed_env()))
    systemctl("daemon-reload")
    systemctl("enable", UNIT)
    systemctl("restart", UNIT)
    print(f"installed {path}\nbrain watch now runs in the background and at every login")
    print(f"log: journalctl --user -u {UNIT}")
    user = os.environ.get("USER") or Path.home().name
    linger = run("loginctl", "show-user", user, "--property=Linger", check=False).stdout.strip()
    if linger == "Linger=no":
        print("it stops when you log out; to keep it running: loginctl enable-linger")


def systemd_uninstall() -> None:
    require_systemd()
    systemctl("disable", "--now", UNIT, check=False)
    unit_path().unlink(missing_ok=True)
    systemctl("daemon-reload")
    print("removed the brain systemd service")


def systemd_status() -> None:
    require_systemd()
    if not unit_path().exists():
        print("not installed (brain service install)")
        return
    proc = systemctl("status", UNIT, "--no-pager", "--lines=5", check=False)
    print(proc.stdout.rstrip() or proc.stderr.rstrip())
