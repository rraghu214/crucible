"""Entry file for NiceGUI's simulated user (see test_nicegui_app.py).

The simulation resets NiceGUI's globals, including registered pages, before
running this file, so the page module is reloaded to register them again.
"""

import importlib

from nicegui import ui

import crucible.ui.nicegui_app as pages

importlib.reload(pages)
ui.run(storage_secret="test-secret")
