"""Build-time selection of isolated OCR/Office runtime files."""
from __future__ import annotations

import json
from pathlib import Path, PurePosixPath


def runtime_data(root: Path) -> list[tuple[str, str]]:
    selected: list[tuple[str, str]] = []
    for engine in ("ocr", "office"):
        folder = root / "tools" / engine
        if not folder.is_dir():
            continue
        excluded: set[str] = set()
        manifest = folder / "notices" / "packaging-manifest.json"
        if engine == "ocr" and manifest.is_file():
            for group in json.loads(manifest.read_text(encoding="utf-8"))["release_payload_exclude"]:
                for item in group["artifacts"]:
                    relative = PurePosixPath(item.replace("\\", "/"))
                    if relative.is_absolute() or ".." in relative.parts or ":" in item:
                        raise ValueError("Runtime exclusion must be a relative payload path")
                    excluded.add(relative.as_posix().casefold())
        for item in sorted(folder.rglob("*")):
            if not item.is_file():
                continue
            relative = item.relative_to(folder)
            # Only top-level archives are installer inputs. Office's nested
            # resource ZIPs are required runtime data and must be retained.
            if len(relative.parts) == 1 and item.suffix.lower() in {".msi", ".zip", ".7z"}:
                continue
            if relative.as_posix().casefold() in excluded:
                continue
            destination = PurePosixPath("tools", engine, *relative.parts[:-1])
            selected.append((str(item), str(destination)))
    return selected


def filter_qt_icu_collisions(entries):
    """Drop incompatible top-level PATH candidates, retaining engine DLLs."""
    blocked = {"icuuc.dll", "icudt78.dll"}
    return [entry for entry in entries
            if entry[0].replace("\\", "/").casefold() not in blocked]
