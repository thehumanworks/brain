# brain

Local full-text search over your Claude Code, Codex and Pi conversation history, for you and
for your agents. Everything stays on your machine: one SQLite file, no server, no sync.

```sh
uv tool install --editable ~/brain   # puts `brain` on your PATH
brain index                          # first run reads every transcript (a few minutes)
brain service install                # keep it current in the background, from every login on
```

`brain service install` runs `brain watch` as you, at low priority, restarting it if it stops:
a launchd agent on macOS (`~/Library/LaunchAgents/local.brain.plist`, log in
`~/Library/Logs/brain.log`) or a systemd user service on Linux and WSL
(`~/.config/systemd/user/brain.service`, log in `journalctl --user -u brain`). WSL needs
systemd turned on in `/etc/wsl.conf`; the command says how if it isn't. `brain service status`
shows whether it is running and its latest log lines; `brain service uninstall` removes it.
Agent directories moved with the variables below are carried into the service when it is
installed. To run it in a terminal instead, use `brain watch`.

## Run directly from GitHub with uvx

With [uv](https://docs.astral.sh/uv/getting-started/installation/) and Git installed, you
can run the CLI without manually cloning the repository, installing `brain` permanently,
or using Nix:

```sh
uvx git+https://github.com/thehumanworks/brain --help
uvx git+https://github.com/thehumanworks/brain index
uvx git+https://github.com/thehumanworks/brain search rate limiting
uvx git+https://github.com/thehumanworks/brain watch --interval 30
```

Arguments after the repository URL go directly to `brain`. uv builds and caches the
application in an isolated environment. Python 3.14+ is required; uv can download a suitable
Python when needed unless automatic downloads are disabled. To select it explicitly:

```sh
uvx --python 3.14 git+https://github.com/thehumanworks/brain --help
```

The index is persistent even though the tool environment is temporary: it uses the same
`~/.local/share/brain/index.db` and transcript locations as any other installation. Use
`--db /absolute/path/index.db` before a subcommand to choose a different index.

To refresh a cached GitHub checkout, or pin a particular revision:

```sh
uvx --refresh git+https://github.com/thehumanworks/brain --help
uvx "git+https://github.com/thehumanworks/brain@<commit-or-tag>" --help
```

For a background service, install persistently rather than calling `service install`
through `uvx`, whose cached executable may be removed during cache cleanup:

```sh
uv tool install git+https://github.com/thehumanworks/brain
brain service install
```

If `brain` is not on PATH after installation, run `uv tool update-shell` and open a new
shell. The existing `pyproject.toml` provides the `brain` console entry point; no PyPI
publication is required.

## Nix

The flake supports `x86_64-linux` and `aarch64-linux`, on NixOS or other Linux distributions
with Nix installed. It supplies Python 3.14 and all required libraries; no system Python,
`uv`, or virtual environment is needed. Enable `nix-command` and `flakes` in your Nix
configuration (on NixOS: `nix.settings.experimental-features = [ "nix-command" "flakes" ];`).

### Run or install

```sh
# From a checkout:
nix run . -- --help
nix run . -- index
nix build .                         # result/bin/brain

# Directly from GitHub:
nix run github:thehumanworks/brain -- search rate limiting
nix profile add github:thehumanworks/brain
```

Both `packages.<system>.default` and `packages.<system>.brain` expose the application;
`apps.<system>.default` and `apps.<system>.brain` expose the CLI. Use `nix shell .` for a
temporary shell with the packaged `brain` on PATH. `nix profile add .` installs a local
checkout instead of the GitHub version.

### Home Manager or NixOS

Add this input to your configuration's `flake.nix`:

```nix
inputs.brain.url = "github:thehumanworks/brain";
```

Pass your flake's `inputs` to modules with `extraSpecialArgs = { inherit inputs; };` for
standalone Home Manager, `specialArgs = { inherit inputs; };` for NixOS, or
`home-manager.extraSpecialArgs = { inherit inputs; };` for Home Manager inside NixOS.
Then install it in a Home Manager module:

```nix
{ inputs, pkgs, ... }:
{
  home.packages = [ inputs.brain.packages.${pkgs.stdenv.hostPlatform.system}.default ];
}
```

Or install it system-wide in a NixOS module:

```nix
{ inputs, pkgs, ... }:
{
  environment.systemPackages = [ inputs.brain.packages.${pkgs.stdenv.hostPlatform.system}.default ];
}
```

No overlay or module is required for package-only installation. The package uses the nixpkgs revision pinned in
this repository's `flake.lock`, independently of your system's Python version. You can set
`inputs.brain.inputs.nixpkgs.follows = "nixpkgs";` to share your configuration's nixpkgs,
but that revision must provide `python314Packages` and the other APIs used by the flake.

### Background indexing with Home Manager

Import the flake's Home Manager module instead of using `brain service install` or writing
a service by hand. With `inputs` passed to your Home Manager configuration as above:

```nix
{ inputs, ... }:
{
  imports = [ inputs.brain.homeManagerModules.default ];

  services.brain = {
    enable = true;
    pollInterval = 30; # seconds; default is 5

    # Optional: use absolute paths for non-default transcript/index locations.
    # environment = {
    #   CODEX_HOME = "/home/alice/custom-codex";
    #   BRAIN_DB = "/home/alice/.local/share/brain/index.db";
    # };
  };
}
```

`homeManagerModules.brain` is an alias for the same module. This is a **Home Manager**
module, usable both standalone and inside NixOS; import it into the Home Manager user's
configuration, not directly into NixOS's system modules.

| Option | Default | Purpose |
|---|---|---|
| `services.brain.enable` | `false` | Install the CLI and enable the systemd user watcher. |
| `services.brain.pollInterval` | `5` | Positive integer seconds between scans and incremental index updates. |
| `services.brain.package` | This flake's package | Override the package installed and run by the service. |
| `services.brain.environment` | `{}` | Environment variables for custom transcript paths or `BRAIN_DB`. |

The watcher indexes immediately on startup, then polls at the configured interval. Each
poll updates changed transcripts; unchanged files are skipped. Scans never overlap. There
is no separate full-rebuild schedule: rebuilding from scratch could lose indexed history
whose original transcripts have been deleted. Environment values are literal (no `$HOME`
or `~` expansion) and stored in the Nix store, so do not put secrets in them.

The service runs at low CPU/I/O priority, starts at login, and restarts after 30 seconds if
it exits. The module defaults `systemd.user.startServices` to `"sd-switch"` so Home Manager
starts or restarts it when you apply changes, including package or interval updates. An
explicit `systemd.user.startServices = false;` (or `"suggest"`) is respected if you prefer
manual service management. The executable remains in the Nix closure across garbage
collection.

If you previously installed the service imperatively, run `brain service uninstall` before
switching to Home Manager. If you used the earlier hand-written Home Manager service
example, replace it with this module rather than keeping both service definitions.
Once managed declaratively, use `systemctl --user status brain` and
`journalctl --user -u brain`, not `brain service install/uninstall`. The service runs as
the logged-in user, not root; it needs a systemd user manager (including on WSL). Configure
user lingering separately if it should run while logged out.

Without Home Manager, install the CLI persistently first, then use `brain service install`
as described above. Do not install the service from a transient `nix run` or `nix shell`:
its executable may be garbage-collected. Re-run `brain service install` after package
updates to refresh the executable path.

## Interactive browsing

For humans, `brain tui` provides a keyboard-driven browser without copying session or
message IDs. Bare `brain` opens the same browser when both input and output are terminals.
Agent-facing commands and their `--json` output are unchanged; piped/no-terminal calls
without a subcommand fail with usage guidance instead of waiting for interactive input.

```sh
brain                                      # recent conversations, newest first
brain tui                                  # explicit interactive command
brain tui rate limiting --agent codex --project api --since 7d
brain --db /path/to/index.db tui
uvx git+https://github.com/thehumanworks/brain tui
nix run . -- tui
```

The list shows conversation titles, agent, date, project path, and search snippets. Type a
query and press **Enter** to search; select a result and press **Enter** again to read it.
Search results open at the exact matched message with surrounding context, not just at
the beginning of a session. Clear the query and press Enter to return to recent conversations.
The Project field filters by path substring. Search uses the same literal words, quoted
phrases, and prefixes as `brain search`; it is submitted with Enter, not on every keystroke.

| Key | Action |
|---|---|
| `Tab` / `Shift-Tab` | Move between search, project filter, and results/conversation. |
| `↑` / `↓`, `Page Up` / `Page Down` | Select results or scroll the conversation. |
| `/` in results | Focus the search field. |
| `Enter` | Submit a search/filter, or open the selected result. |
| `Esc` | Return to results, preserving the query and selection; in results, focus search. |
| `F2` | Cycle the agent filter: all, Claude, Codex, Pi. |
| `m` in results | Load another 100 results when more are available. |
| `F3` in the conversation | Hide/show tool calls and output; a matched tool message stays visible. |
| `F6` / `F7` in the conversation | Previous/next page of messages. |
| `F5` | Refresh from the index, including updates made by `brain watch`. |
| `Ctrl-Q` / `Ctrl-C` | Quit. |

Recent conversations omit subagents by default (`brain tui --all` includes them); search
can find their messages. Search shows up to two hits per session; opening one lets you
page through the conversation. Results load in batches of 100, and conversations in pages
of 30 messages. Messages are not shortened in the reader beyond the index's existing 64 KiB
per-message cap.

The browser is **read-only**: run `brain index` first or keep `brain watch` running separately.
`F5` reloads the database, not the transcript files. Empty, missing, or temporarily locked
indexes produce guidance in the UI and can be retried without restarting it. SQLite work
runs off the UI event loop, cancelled searches are interrupted, and obsolete results cannot
overwrite a newer search. Transcript text is rendered literally, with terminal control
sequences neutralized.

The TUI uses [prompt_toolkit](https://python-prompt-toolkit.readthedocs.io/): a lightweight
terminal library with Unicode, wrapping, keyboard handling, and headless input testing.
It is imported only when opening the browser, so CLI startup for agents stays lean.

## Searching

```sh
brain search rate limiting                  # every word must match, best first
brain search '"connection refused"' --since 7d
brain search deploy* --agent codex --project api
brain search oomkilled --role tool          # only tool output
brain list                                  # the 20 most recent sessions (-n for more, --all for subagents)
brain show 3f2a91c0 --at 81234              # open a hit: session id + message id from search
brain show 3f2a91c0 --no-tools              # just the conversation
brain stats
```

Add `--json` to `search`, `list` or `show` for machine-readable output.

Queries are words, `"quoted phrases"` and `prefix*`; punctuation is taken literally, so
`brain search foo.py` works (put `--` before a query that starts with a dash:
`brain search -- --force-with-lease`). `--any` matches any word
instead of all of them, and `--raw` passes the query to
[SQLite FTS5](https://sqlite.org/fts5.html#full_text_query_syntax) untouched (`NEAR`, `OR`,
column filters). Results are ranked with BM25, with tool output and injected context ranked
below what you and the agent actually said.

## Letting agents use it

Add this to `~/.claude/CLAUDE.md`, `~/.codex/AGENTS.md` and Pi's `~/.pi/agent/AGENTS.md`:

```markdown
## Past conversations
`brain` searches every past Claude Code, Codex and Pi session on this machine. Use it when the
user refers to earlier work ("like we did last week", "that bug from before"), or before
redoing an investigation that may already have been done.
- `brain search <words> [--project <dir>] [--since 7d] [--agent claude|codex|pi]`
- `brain list [-n 20] [--project <dir>]` for the most recent sessions
- `brain show <session> --at <msg> -C 5` to read around a hit; `--no-tools` for just the talk
```

## What it reads

| Agent | Transcripts | Moved with |
|---|---|---|
| Claude Code | `~/.claude/projects/*/*.jsonl`, plus `*/subagents/` | `CLAUDE_CONFIG_DIR` |
| Codex | `~/.codex/sessions/`, `~/.codex/archived_sessions/` (`.jsonl` and `.jsonl.zst`) | `CODEX_HOME` |
| Pi | `~/.pi/agent/sessions/`, `~/.pi/agent/bots/*/sessions/` | `PI_CODING_AGENT_DIR` |

User prompts, assistant replies, thinking, tool calls and tool output are all indexed; each
message is clipped to 64 KiB. Codex's encrypted reasoning and its repeated system prompt are
skipped. The index lives at `~/.local/share/brain/index.db` (`BRAIN_DB` or `--db` to move it)
and can be deleted and rebuilt at any time.

## How it keeps up

Each transcript has a checkpoint: how many bytes were read and a fingerprint of the last line.
A run skips files whose size and modification time haven't changed, reads only the new lines
of the rest, and starts a file over if the bytes at its checkpoint no longer match (the
transcript was rewritten). A line still being written is left for the next run. Codex
transcripts are tracked by thread id, so a rollout that Codex compresses or archives carries on
from where it was. Messages are keyed by their time and content, so nothing is stored twice,
even when an agent writes a message out again under a new id.

Indexing never changes or deletes a transcript, and nothing is removed from the index when an
agent deletes its own old sessions: Claude Code's cleanup no longer loses your history. A
rebuild from scratch only finds the transcripts still on disk, so keep the index once it holds
sessions whose transcripts are gone.

## Development

```sh
nix develop
python -m brain --help
python -m unittest discover -s tests -v
nix fmt
nix flake check
```

The development shell uses Python 3.14 and imports `src/` from the checkout, and includes
`uv` and the Nix formatter. No dependencies are installed into the host system.
`nix flake check` builds the package, runs the unit tests (including SQLite FTS5 and zstd
coverage), and smoke-tests the installed CLI with an isolated home directory. TUI tests
use actual key sequences through prompt_toolkit's pipe input and real SQLite/FTS fixtures:
search-to-message navigation, wrapped-paragraph scrolling, paging, filters, refresh, error
recovery, stale-request races, read-only access, and headless CLI compatibility. A POSIX
pseudo-terminal test also launches bare `brain`, searches, opens a hit, and checks terminal
restoration on exit. Tests never open your personal index.
The flake also tests
the module against the pinned Home Manager input: disabled/enabled defaults, overrides,
interval validation, environment quoting, and generated unit files. It builds checks for
the current architecture; testing the other architecture requires a matching builder.

`flake.lock` pins nixpkgs and Home Manager for reproducibility. Home Manager is an input
for module checks, not a runtime dependency of the CLI. To update them deliberately, run
`nix flake update nixpkgs home-manager`, then `nix flake check`. When adding files in a Git checkout,
remember that Nix flakes only include files known to Git.

Without Nix:

```sh
uv run python -m unittest discover -s tests -v
```

Requires Python 3.14+ (for `compression.zstd`) and `prompt-toolkit` 3.x. Both the Python
package metadata and Nix package declare the terminal dependency.
