"""Entry point of the frozen application: no arguments opens the GUI."""

import multiprocessing
import sys

from flashtool.cli import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main(sys.argv[1:] or ["gui"]))
