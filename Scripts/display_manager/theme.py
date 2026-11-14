# =============================================================================
# theme.py - Dracula colour palette and application CSS
# =============================================================================
# Same palette and libadwaita variable overrides as sound_manager/theme.py
# and network_manager/theme.py, so all MrFaws apps look alike. The palette
# constants are also used by layout.py, which draws with cairo.
#
# libadwaita draws every widget from a set of CSS custom properties
# (--window-bg-color, --accent-bg-color, ...). Instead of restyling each
# widget by hand, we override those variables with Dracula colours. This
# keeps all stock widgets (rows, switches, dialogs, toasts) consistent and
# means new widgets automatically pick up the theme.
# =============================================================================

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gtk


# -----------------------------------------------------------------------------
# Dracula palette
# -----------------------------------------------------------------------------
# The official Dracula colours. BACKGROUND/SURFACE/FOREGROUND and the accent
# colours were specified for this project; COMMENT, RED, ORANGE and YELLOW
# are the remaining official Dracula colours, used for borders and states.
BACKGROUND = "#282a36"
BACKGROUND_DARK = "#21222c"   # Dracula "darker" variant, used for sidebar/header
SURFACE = "#44475a"           # Dracula "current line"
COMMENT = "#6272a4"
FOREGROUND = "#f8f8f2"
PURPLE = "#bd93f9"
CYAN = "#8be9fd"
GREEN = "#50fa7b"
PINK = "#ff79c6"
RED = "#ff5555"
ORANGE = "#ffb86c"
YELLOW = "#f1fa8c"


CSS = f"""
/* ---------------------------------------------------------------------------
   libadwaita colour variables -> Dracula
   --------------------------------------------------------------------------- */
:root {{
    --window-bg-color: {BACKGROUND};
    --window-fg-color: {FOREGROUND};
    --view-bg-color: {BACKGROUND};
    --view-fg-color: {FOREGROUND};

    --headerbar-bg-color: {BACKGROUND_DARK};
    --headerbar-fg-color: {FOREGROUND};
    --headerbar-backdrop-color: {BACKGROUND_DARK};
    --headerbar-shade-color: rgba(0, 0, 0, 0.36);

    --sidebar-bg-color: {BACKGROUND_DARK};
    --sidebar-fg-color: {FOREGROUND};
    --sidebar-backdrop-color: {BACKGROUND_DARK};
    --secondary-sidebar-bg-color: {BACKGROUND_DARK};

    /* Cards and boxed-list rows use the Dracula surface colour. */
    --card-bg-color: {SURFACE};
    --card-fg-color: {FOREGROUND};
    --card-shade-color: rgba(0, 0, 0, 0.36);

    --dialog-bg-color: {BACKGROUND};
    --dialog-fg-color: {FOREGROUND};
    --popover-bg-color: {BACKGROUND_DARK};
    --popover-fg-color: {FOREGROUND};

    --accent-bg-color: {PURPLE};
    --accent-fg-color: {BACKGROUND};
    --accent-color: {PURPLE};

    --success-bg-color: {GREEN};
    --success-fg-color: {BACKGROUND};
    --success-color: {GREEN};

    --warning-bg-color: {ORANGE};
    --warning-fg-color: {BACKGROUND};
    --warning-color: {ORANGE};

    --error-bg-color: {RED};
    --error-fg-color: {FOREGROUND};
    --error-color: {RED};

    --destructive-bg-color: {RED};
    --destructive-fg-color: {FOREGROUND};
    --destructive-color: {RED};
}}

/* ---------------------------------------------------------------------------
   Brightness card
   --------------------------------------------------------------------------- */
.hero {{
    padding: 22px 24px;
    border-radius: 16px;
}}
.hero .hero-title {{
    font-size: 1.45em;
    font-weight: 800;
}}
.hero .hero-icon {{
    color: {YELLOW};
    background-color: alpha({YELLOW}, 0.14);
    border-radius: 999px;
    padding: 14px;
}}
scale.brightness trough {{
    min-height: 10px;
    border-radius: 999px;
    background-color: alpha({FOREGROUND}, 0.12);
}}
scale.brightness highlight {{
    border-radius: 999px;
    background-image: linear-gradient(to right, {PURPLE}, {YELLOW});
}}
scale.brightness slider {{
    min-width: 24px;
    min-height: 24px;
    background-color: {FOREGROUND};
    box-shadow: 0 1px 4px rgba(0, 0, 0, 0.45);
}}
.percent-label {{
    font-size: 1.2em;
    font-weight: 800;
    font-feature-settings: "tnum";
    min-width: 3.4em;
}}
button.preset {{
    font-feature-settings: "tnum";
    border-radius: 999px;
    padding: 2px 12px;
}}

/* ---------------------------------------------------------------------------
   Arrangement canvas
   --------------------------------------------------------------------------- */
.layout-card {{
    border-radius: 16px;
    padding: 6px;
    background-color: {BACKGROUND_DARK};
}}

/* Small pill badges ("Primary", "Off", ...). */
.badge {{
    font-size: 0.8em;
    font-weight: 700;
    padding: 2px 9px;
    border-radius: 999px;
    background-color: alpha({COMMENT}, 0.45);
    color: {FOREGROUND};
}}
.badge.active {{
    background-color: alpha({GREEN}, 0.18);
    color: {GREEN};
}}
.badge.off {{
    background-color: alpha({RED}, 0.18);
    color: {RED};
}}

.hint {{
    color: alpha({FOREGROUND}, 0.65);
    font-size: 0.9em;
}}
.countdown {{
    font-feature-settings: "tnum";
}}
"""


def apply_theme() -> None:
    """Install the Dracula stylesheet for the whole application.

    Must run after Gtk/Adw have been initialised (i.e. from
    Application.do_startup), because it needs the default display.
    """
    # Dracula is a dark theme. Force libadwaita's dark variant so any colour
    # we did not override (e.g. shadows, scrollbars) also stays dark.
    Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)

    provider = Gtk.CssProvider()
    provider.load_from_string(CSS)

    # USER priority sits above libadwaita's own stylesheet, so our variable
    # overrides win over the defaults.
    Gtk.StyleContext.add_provider_for_display(
        Gdk.Display.get_default(),
        provider,
        Gtk.STYLE_PROVIDER_PRIORITY_USER,
    )
