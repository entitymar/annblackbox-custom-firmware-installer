#!/usr/bin/env python3
"""annblackbox-custom-firmware-installer. Created by entitymar."""
import sys
from pathlib import Path

# -I isolates dependencies; this exact application directory owns the modules.
sys.path.insert(0, str(Path(__file__).resolve().parent))

def main():
    from blackbox.app import main as application_main
    return application_main()

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        import traceback
        message = str(exc)
        try:
            path = Path(__file__).resolve().parent / "logs" / "startup.log"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as out:
                out.write(traceback.format_exc()+"\n")
            message += "\n\nLog: "+str(path)
        except OSError:
            pass
        if sys.stderr:
            print(message, file=sys.stderr)
        try:
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror("annblackbox-custom-firmware-installer", message)
            root.destroy()
        except Exception:
            pass
        raise SystemExit(1)
