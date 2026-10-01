import os
import logging
import sys
from .web import create_app
from .accounts import AccountError, github_preflight
from .config import load_config, remove_legacy_credentials


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        github_preflight()
        load_config()
        remove_legacy_credentials()
        app = create_app()
    except (ValueError, FileNotFoundError, AccountError, OSError) as exc:
        sys.exit(f"Cannot start Dev-Cockpit: {exc}")
    app.run(host=os.getenv("HOST", "127.0.0.1"), port=int(os.getenv("PORT", "7777")), debug=False)


if __name__ == '__main__':
    main()
