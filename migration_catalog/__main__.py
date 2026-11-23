"""Allow ``python -m migration_catalog``."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
