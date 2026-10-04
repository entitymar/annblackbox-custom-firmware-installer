"""Windows double-click launcher: one native window, no console window."""
import runpy
from pathlib import Path

runpy.run_path(str(Path(__file__).with_suffix(".py")), run_name="__main__")
