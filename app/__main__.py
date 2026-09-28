import os
import logging
import sys
from .web import create_app


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        app = create_app()
    except (ValueError, FileNotFoundError) as exc:
        sys.exit(f"Cannot start Dev-Cockpit: {exc}")
    app.run(host=os.getenv("HOST", "127.0.0.1"), port=int(os.getenv("PORT", "7777")), debug=False)


if __name__ == '__main__':
    main()
