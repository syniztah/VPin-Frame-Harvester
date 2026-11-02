"""Where the harvester's own files live (games/, out/, settings).

Running from source: the repository folder. Running as the packaged Windows
app: the folder that holds the .exe, so game files and dumps stay next to it
and can be edited and found by the user."""
import os
import sys

FROZEN = getattr(sys, "frozen", False)
BASE = os.path.dirname(os.path.abspath(sys.executable)) if FROZEN else \
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
