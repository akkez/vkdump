"""PyInstaller entry point. Bridges the packaged GUI main() into a flat script."""
import sys

from vkdump.gui.app import main


if __name__ == "__main__":
    sys.exit(main())
