"""PyInstaller entry point. Bridges the packaged CLI app into a flat script."""
from vkdump.cli.main import app


if __name__ == "__main__":
    app()
