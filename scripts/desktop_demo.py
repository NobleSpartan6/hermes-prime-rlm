"""Try the graphical controls without installing Hermes or calling a provider."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "hermes_prime_demo", root / "__init__.py", submodule_search_locations=[str(root)],
    )
    if spec is None or spec.loader is None:
        print("Cannot locate the plugin. Run this script from a complete source checkout.")
        return 2
    package = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = package
    spec.loader.exec_module(package)
    module = __import__(f"{spec.name}.desktop_ui", fromlist=["launch_ui"])
    return module.launch_ui(demo=True)


if __name__ == "__main__":
    raise SystemExit(main())
