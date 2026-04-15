"""PyInstaller entry point — imports chartster.gui as a package so
relative imports inside the package resolve correctly."""
from chartster.gui import main

if __name__ == "__main__":
    main()
