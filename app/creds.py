"""The app's two secrets (the Anthropic API key and the Telegram bot token) and where they can come from.

Each can be saved in the app under Settings (stored in the settings table of stock.db), or given to the container:
  ANTHROPIC_API_KEY / TELEGRAM_BOT_TOKEN            the value itself, or
  ANTHROPIC_API_KEY_FILE / TELEGRAM_BOT_TOKEN_FILE  a file holding it (Docker/compose secrets: /run/secrets/<name>).
A value saved in Settings wins; removing it there falls back to the container's value.
Saved secrets are taken out of every backup copy (db.scrub), so a copied backup never carries them.
"""
import os

# settings-table keys that hold a secret; never sent to a client and never kept in a backup
SECRET_SETTINGS = ("anthropic_api_key", "tg_token")

_warned = set()


def from_env(name):
    """NAME from the environment, else the contents of the file named by NAME_FILE (trimmed). "" if neither is set."""
    v = os.environ.get(name, "").strip()
    if v:
        return v
    path = os.environ.get(name + "_FILE", "").strip()
    if not path:
        return ""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except (OSError, UnicodeDecodeError) as e:
        if path not in _warned:  # the bot asks every few seconds: say it once, not every time
            _warned.add(path)
            print("magpie: can't read %s_FILE (%s): %s" % (name, path, getattr(e, "strerror", None) or e), flush=True)
        return ""
