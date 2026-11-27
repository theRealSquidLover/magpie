# Development security setup

What this repo does, and the parts that live outside it and have to be set up by hand on your own machine and account.

Source: crftd.tech, "Notes on safer development" (2026-05-30) and "part 1.5: VSCode and Claude" (2026-10-30).

## In the repo

| File | What it does |
|---|---|
| `CLAUDE.md` | Supply-chain rules Claude follows in this repo: pinned versions, hashes, a 7-day cooldown (14 for Docker tags), no curl-pipe-shell, stop instead of falling back. |
| `requirements.in`, `requirements-dev.in` | The direct dependencies. Edit these. |
| `requirements.txt`, `requirements-dev.txt` | Hashed lockfiles generated from the `.in` files with `uv pip compile` (the command is in `CLAUDE.md`). Don't edit them by hand. |
| `Dockerfile` | Base image pinned by digest. Installs with `--require-hashes --only-binary=:all:`. |
| `.github/workflows/build.yml` | CI installs with hashes required, wheels only. |
| `.vscode/settings.json` | Turns off automatic tasks and task auto-detection for this folder. |
| `.devcontainer/` | A container to develop in, on the same pinned base, with no SSH agent passed in. |

Install a dev environment on the host (if you don't use the container):

```
python3.12 -m venv .venv && . .venv/bin/activate
pip install --require-hashes --only-binary=:all: -r requirements-dev.txt
```

## VS Code user settings (paste by hand)

VS Code ignores several of the article's settings when they're in a workspace file, so these belong in your **User** settings (Command Palette, "Preferences: Open User Settings (JSON)"):

```jsonc
{
  "update.mode": "none",
  "extensions.autoUpdate": false,
  "extensions.autoCheckUpdates": false,
  "extensions.ignoreRecommendations": true,
  "task.allowAutomaticTasks": "off",
  "task.autoDetect": "off",
  "npm.autoDetect": "off",
  "dev.containers.dockerCredentialHelper": false,
  "dev.containers.copyGitConfig": false,
  "dev.containers.gitCredentialHelperConfigLocation": "none"
}
```

With `update.mode` set to `none`, VS Code no longer updates itself. Update it by hand now and then, after a new release has been out for about a week.

### Optional: allow only pinned extensions

The article also locks extensions to exact versions with `extensions.allowed`. Every extension not listed gets disabled, so build the list from what you already use: run `code --list-extensions --show-versions`, then add each one:

```jsonc
"extensions.allowed": {
  "*": false,
  "ms-python.python": ["<version from the list>"],
  "ms-vscode-remote.remote-containers": ["<version>"]
}
```

Send that list to Claude and it can pin the same versions in `.devcontainer/devcontainer.json`.

### SSH and GPG agents

On Linux with the Flatpak build of VS Code, the article blocks the agents with:

```
flatpak override --nosocket=ssh-auth --nosocket=gpg-agent --unset-env=SSH_AUTH_SOCK com.visualstudio.code
```

On macOS or Windows there's no equivalent switch. The devcontainer clears `SSH_AUTH_SOCK`, and the user settings above stop git and Docker credentials being copied in.

## Claude Code on your machine (`~/.claude/settings.json`)

Stop Claude Code from updating itself and from installing its VS Code extension on its own:

```json
{
  "env": {
    "DISABLE_AUTOUPDATER": "1",
    "CLAUDE_CODE_IDE_SKIP_AUTO_INSTALL": "1"
  }
}
```

The article also sets `DISABLE_UPDATES`. That name isn't in the Claude Code settings docs as far as checked here, so it's left out. You then update Claude Code by hand. The article's `install-claude.sh` checks Anthropic's signed release manifest before installing a pinned version; it isn't copied here.

## Text for Claude's user memory or `~/.claude/CLAUDE.md`

This applies the same rules to every project, not just this one:

```
Supply-chain rules (all projects):
- Pin every dependency to an exact version (with a lockfile and hashes where the tool supports it). Never use floating versions, @latest, or tags without a digest for container images.
- Don't run remote packages directly (npx, uvx, pipx run, bunx). Pin, install, then run.
- Wait at least 7 days after a release before adopting it (14 for mutable sources like Docker tags). A vulnerability fix may use a release at least 2 days old.
- Disable install scripts and build hooks wherever the tool allows (npm ignore-scripts, pip --only-binary=:all:).
- Never pipe a download into a shell or interpreter. If I paste one, explain the risk and offer to download, review, pin and install it instead.
- If a rule can't be met, stop and tell me why. Don't fall back to a less safe option.
```
