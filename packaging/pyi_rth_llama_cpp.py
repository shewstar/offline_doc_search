"""PyInstaller runtime hook — make llama.dll discoverable in frozen builds."""

import os
import sys

if getattr(sys, "frozen", False):
    base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    lib = os.path.join(base, "llama_cpp", "lib")
    if os.path.isdir(lib):
        os.add_dll_directory(lib)
