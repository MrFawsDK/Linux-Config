#!/usr/bin/env python3
# =============================================================================
# MrFaws Network Manager V2 - entry point
# =============================================================================
# This script is what the desktop launcher (mrfaws-wifi.desktop) runs. It only
# makes the network_manager package importable and starts the GTK application;
# all real logic lives in Scripts/network_manager/.
#
# V1 NOTE:
#   The complete V1 implementation (GTK3 + nmcli) that used to live in this
#   file has been moved unchanged to Scripts/legacy/wifi_gui_v1.py.
#   Reason: V1 was replaced by the libnm based V2 package. The archived file
#   is never imported by V2. It remains available there and in Git history
#   (main branch, commit 53d8d11).
# =============================================================================

import os
import sys

# The package sits next to this script. The launcher runs this file by
# absolute path, so the working directory can be anything; add the script's
# own directory to the import path explicitly.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from network_manager.app import main  # noqa: E402  (import after path setup)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
