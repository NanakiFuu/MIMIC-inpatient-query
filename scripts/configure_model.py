"""Interactive secret entry; run as normal Ubuntu user, never through captured input."""
import getpass
import json
import os
from pathlib import Path
import pwd
import tempfile

CONFIG = Path.home() / ".config/medical-query/model.json"


def main():
    if os.getuid() == 0:
        raise SystemExit("Run as the normal Ubuntu user, without sudo")
    print("Provider: DeepSeek; model ID: deepseek-flash (DeepSeek-V4.1-Flash)")
    print("Base URL: https://api.deepseek.com; key will not be displayed or logged.")
    key = getpass.getpass("DeepSeek API Key: ").strip()
    if not key or any(char.isspace() for char in key):
        raise SystemExit("Empty key or whitespace; nothing saved")
    if CONFIG.is_symlink() or CONFIG.parent.is_symlink():
        raise SystemExit("Refusing symlink config path")
    CONFIG.parent.mkdir(parents=True,exist_ok=True)
    os.chmod(CONFIG.parent,0o700)
    fd, name = tempfile.mkstemp(prefix=".model-",dir=CONFIG.parent)
    try:
        with os.fdopen(fd,"w") as stream:
            json.dump({"provider":"deepseek","base_url":"https://api.deepseek.com",
                       "model":"deepseek-flash","api_key":key,"timeout_seconds":25},stream)
        os.chmod(name,0o600)
        os.replace(name,CONFIG)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    print(f"Saved private config: {CONFIG} (0600). Do not copy its contents into chat.")
    print("The application reads it on demand; no service restart is needed for the key.")


if __name__=="__main__":
    main()
