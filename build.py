#!/usr/bin/env python3
"""Package the addon into a Kodi-installable zip.

Kodi requires the zip to contain a single top-level directory named exactly
after the addon id, so the archive paths are rewritten accordingly. The output
name embeds the version from addon.xml, which is also what Kodi compares to
decide an install is an upgrade rather than a fresh install.
"""

import shutil
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"

# Anything matching these is developer scaffolding, not part of the addon.
EXCLUDED_DIRS = {".git", ".github", "dist", "tests", "tools", "__pycache__", ".idea", ".vscode"}
# PERF.md and the issue template are for contributors, not users. README
# stays: Kodi shows it in the addon information dialog.
EXCLUDED_FILES = {"build.py", "debug.py", "pydevd-pycharm.egg", ".gitignore",
                  ".DS_Store", "PERF.md"}
EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".zip"}


def read_addon_metadata():
    addon_xml = ROOT / "addon.xml"
    root = ElementTree.parse(addon_xml).getroot()
    addon_id = root.get("id")
    version = root.get("version")
    if not addon_id or not version:
        sys.exit("addon.xml is missing an id or version attribute")
    return addon_id, version


def should_include(path: Path) -> bool:
    relative = path.relative_to(ROOT)
    # Anything hidden is tooling, not addon content. This is a blanket rule
    # rather than a list of names because the list is what fails: `.claude/`
    # appeared mid-project and would otherwise have shipped to users.
    if any(part.startswith(".") for part in relative.parts):
        return False
    if EXCLUDED_DIRS.intersection(relative.parts):
        return False
    if relative.name in EXCLUDED_FILES:
        return False
    return relative.suffix not in EXCLUDED_SUFFIXES


def collect_files():
    return sorted(p for p in ROOT.rglob("*") if p.is_file() and should_include(p))


def build():
    addon_id, version = read_addon_metadata()
    DIST.mkdir(exist_ok=True)
    target = DIST / f"{addon_id}-{version}.zip"

    files = collect_files()
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        # Kodi's Android zip VFS does not infer a parent directory from its
        # files. Without this concrete entry it can list the archive yet fails
        # to open plugin.video.immich/addon.xml during installation.
        directory = zipfile.ZipInfo(f"{addon_id}/")
        directory.create_system = 3
        directory.external_attr = (0o40755 << 16) | 0x10
        archive.writestr(directory, b"")
        for path in files:
            archive.write(path, Path(addon_id) / path.relative_to(ROOT))

    # A stable filename makes repeated side-loading from the same path easy.
    shutil.copyfile(target, DIST / f"{addon_id}.zip")

    print(f"{target}  ({len(files)} files, {target.stat().st_size // 1024} KiB)")
    print(f"{DIST / f'{addon_id}.zip'}  (stable alias)")


if __name__ == "__main__":
    build()
