"""Entry point used by Briefcase: the app is launched as ``python -m bookinventory``."""

from bookinventory.app import main

if __name__ == "__main__":
    main().main_loop()
