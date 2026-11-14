#!/usr/bin/env python3
# =============================================================================
# MrFaws Sound Manager - entry point
# =============================================================================
# This script is what the desktop launcher (mrfaws-sound.desktop), the
# `sound` command and the Waybar volume module run. It only makes the
# sound_manager package importable and starts the GTK application; all real
# logic lives in Scripts/sound_manager/.
# =============================================================================

import os
import sys

# The package sits next to this script. The `sound` command reaches it
# through a symlink in ~/.local/bin, so realpath() follows that back to
# Scripts/ before adding it to the import path.
SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from sound_manager.app import main  # noqa: E402  (import after path setup)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
