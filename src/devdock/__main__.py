"""Allow ``python -m devdock`` in addition to the installed ``devdock`` script."""

from devdock.cli import app

if __name__ == "__main__":
    app()
