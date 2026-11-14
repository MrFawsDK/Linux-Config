#!/usr/bin/env python3
# =============================================================================
# MrFaws Display Manager - entry point
# =============================================================================
# This script is what the desktop launcher (mrfaws-display.desktop) and the
# `screen` command run. All real logic lives in Scripts/display_manager/.
#
#   screen                        open the window
#   screen brightness             print the laptop brightness in percent
#   screen brightness 40          set it to 40 %
#   screen brightness +5 -5       step up/down (used by the brightness keys,
#                                 add --notify for an on-screen bubble)
#
# The brightness command never imports GTK, so the keys react instantly.
# =============================================================================

import os
import sys

# The package sits next to this script. The `screen` command reaches it
# through a symlink in ~/.local/bin, so realpath() follows that back to
# Scripts/ before adding it to the import path.
SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "brightness":
        from display_manager.backlight import cli  # noqa: E402
        sys.exit(cli(sys.argv[2:]))

    from display_manager.app import main  # noqa: E402  (import after path setup)
    sys.exit(main(sys.argv))
