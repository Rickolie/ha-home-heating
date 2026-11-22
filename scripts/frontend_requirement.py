"""Print the home-assistant-frontend requirement of the installed Home Assistant (for the tests)."""

import json
from pathlib import Path

import homeassistant

manifest = Path(homeassistant.__file__).parent / "components" / "frontend" / "manifest.json"
print(json.loads(manifest.read_text())["requirements"][0])
