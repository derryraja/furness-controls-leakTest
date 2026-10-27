"""
Leak Tester -- entry point.

    python app.py              # use settings from config.json
    python app.py --simulate   # force simulation mode (no hardware)
"""

import argparse

from leaktest.config import Config
from leaktest.gui import LeakTestApp


def main():
    parser = argparse.ArgumentParser(description="F-BUS leak test station")
    parser.add_argument("--simulate", action="store_true",
                        help="run with the built-in simulator instead of hardware")
    parser.add_argument("--config", default=None, help="path to config.json")
    args = parser.parse_args()

    cfg = Config.load(args.config) if args.config else Config.load()
    LeakTestApp(cfg, force_simulate=args.simulate).mainloop()


if __name__ == "__main__":
    main()
