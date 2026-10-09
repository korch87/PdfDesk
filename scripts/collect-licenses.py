"""Collect the runtime notices and provenance inventory for a PdfDesk build.

The collector is driven by the actual build inventory instead of a hard-coded
package list:

* declared dependencies come from ``app/requirements.txt`` (Windows) and
  ``app/requirements-macos.txt`` (macOS);
* the installed distributions of the interpreter that runs the collector are
  scanned, including the transitive runtime closure (so a package such as
  charset-normalizer that is pulled in by reportlab is collected even though it
  is not pinned);
* build-only packages (PyInstaller and its toolchain) are reported separately;
* the Python runtime's own license is collected from the interpreter prefix;
* staged engine notices are collected from ``app/tools/ocr/notices`` and
  ``app/tools/office``.

It never invents a license: each package records only what its own metadata
declares, and a package whose metadata declares nothing is reported as
UNRESOLVED and flagged. Installer inputs (``.msi``/``.zip``/``.7z``/``.cab``/
``.iso``) are never copied and are reported when found in a scanned tree.

Usage:
    python scripts/collect-licenses.py app/LICENSES            # write
    python scripts/collect-licenses.py app/LICENSES --check    # verify fidelity
    python scripts/collect-licenses.py app/LICENSES --strict   # fail on flags
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata as metadata
import json
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DESTINATION = PROJECT_ROOT / 'app' / 'LICENSES'
REPORT_NAME = 'COLLECTION-REPORT.json'
LICENSE_NAME_TOKENS = ('license', 'licence', 'copying', 'notice')
ATTRIBUTION_NAME_TOKENS = LICENSE_NAME_TOKENS + (
    'readme', 'authors', 'third_party', 'third-party', 'eula', 'credits',
    'copyright', 'attribution', 'mpl', 'lgpl', 'gpl', 'about')
ARCHIVE_SUFFIXES = ('.msi', '.zip', '.7z', '.cab', '.iso')
INSTALLER_INPUT_SUFFIXES = ('.msi', '.7z', '.cab', '.iso')
OFFICE_ROOT_NOTICES = ('LICENSE.html', 'license.txt', 'NOTICE')
OCR_NOTICES_DIR = Path('tools') / 'ocr' / 'notices'
MAX_NOTICE_BYTES = 4 * 1024 * 1024


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def is_notice_name(name: str) -> bool:
    lowered = name.lower()
    return any(token in lowered for token in LICENSE_NAME_TOKENS)


def is_attribution_name(name: str) -> bool:
    lowered = name.lower()
    return any(token in lowered for token in ATTRIBUTION_NAME_TOKENS)


def parse_requirements(path: Path) -> list[dict]:
    declared = []
    if not path.is_file():
        return declared
    for raw in path.read_text(encoding='utf-8').splitlines():
        line = raw.split('#', 1)[0].strip()
        if not line or line.startswith('-'):
            continue
        name, separator, pin = line.partition('==')
        declared.append({'name': name.strip(), 'pin': pin.strip() if separator else None})
    return declared


def normalize(name: str) -> str:
    return name.strip().lower().replace('_', '-').replace('.', '-')


def requirement_names(distribution: metadata.Distribution) -> list[str]:
    """Runtime requirement names, skipping extra-gated (test/tooling) entries."""
    names = []
    for requirement in distribution.requires or ():
        head, separator, marker = requirement.partition(';')
        if separator and 'extra' in marker.lower():
            continue
        head = head.strip()
        for splitter in ('[', '(', '<', '>', '=', '!', '~', ' '):
            head = head.split(splitter, 1)[0]
        head = head.strip()
        if head and not head.startswith('-'):
            names.append(head)
    return names


def environment_distributions() -> tuple[dict[str, metadata.Distribution], dict[str, list[str]]]:
    """Installed distributions, with duplicate installs recorded.

    The audited build interpreter injects a second site-packages directory, so
    several packages exist twice. The copy inside ``sys.prefix`` wins and every
    duplicate is reported so the choice stays visible.
    """
    grouped: dict[str, list[metadata.Distribution]] = {}
    for distribution in metadata.distributions():
        name = distribution.metadata.get('Name')
        if name:
            grouped.setdefault(normalize(name), []).append(distribution)
    chosen: dict[str, metadata.Distribution] = {}
    duplicates: dict[str, list[str]] = {}
    prefix = str(Path(sys.prefix).resolve()).lower()
    for name, options in grouped.items():
        ordered = sorted(options, key=lambda item: str(item._path))
        preferred = next((item for item in ordered
                          if str(item._path).lower().startswith(prefix)), ordered[0])
        chosen[name] = preferred
        if len(ordered) > 1:
            duplicates[name] = [str(item._path) for item in ordered]
    return chosen, duplicates


def runtime_closure(declared: list[dict], installed: dict[str, metadata.Distribution]) -> tuple[list[str], list[str]]:
    """Resolve declared packages plus their installed transitive requirements."""
    closure: list[str] = []
    unpinned: list[str] = []
    queue = [normalize(item['name']) for item in declared]
    declared_names = set(queue)
    while queue:
        name = queue.pop(0)
        if name in closure:
            continue
        distribution = installed.get(name)
        if distribution is None:
            continue
        closure.append(name)
        for requirement in requirement_names(distribution):
            key = normalize(requirement)
            if key not in closure and key in installed:
                if key not in declared_names and key not in unpinned:
                    unpinned.append(key)
                queue.append(key)
    return sorted(closure), sorted(unpinned)


def license_files_of(distribution: metadata.Distribution) -> list[dict]:
    paths = []
    for relative in distribution.files or ():
        # Match the whole recorded path: distributions keep notices in a
        # directory that may itself be the only "license" token.
        if not is_notice_name(str(relative)):
            continue
        candidate = Path(distribution.locate_file(relative))
        if candidate.is_file():
            paths.append({'relative': Path(str(relative).replace('/', '\\')),
                          'source': candidate})
    return sorted(paths, key=lambda item: str(item['relative']).lower())


def flattened_notice_name(relative: Path, index: int) -> str:
    """Short deterministic file name for a notice taken out of a distribution."""
    candidate = str(relative).replace('\\', '__').replace('/', '__')
    if len(candidate) <= 110:
        return candidate
    return f'{index:02d}__{relative.name[:80]}'


def distribution_entry(distribution: metadata.Distribution, *, category: str,
                       display_name: str | None = None) -> dict:
    expression = (distribution.metadata.get('License-Expression')
                  or distribution.metadata.get('License') or '').strip()
    classifiers = [value for value in distribution.metadata.get_all('Classifier') or ()
                   if value.lower().startswith('license')]
    files = license_files_of(distribution)
    return {
        'name': display_name or distribution.metadata.get('Name'),
        'metadata_name': distribution.metadata.get('Name'),
        'version': distribution.version,
        'category': category,
        'location': str(distribution._path),
        'license_metadata': expression or None,
        'license_classifiers': classifiers,
        'license_files': [{'source': str(item['source']), 'relative': str(item['relative']),
                           'sha256': sha256_of(item['source']),
                           'size': item['source'].stat().st_size} for item in files],
    }


def copy_file(source: Path, destination: Path, records: list[dict], *, category: str,
              relative: Path | None = None, note: str | None = None) -> Path:
    target = destination / (relative if relative is not None else Path(source.name))
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    records.append({
        'path': target.relative_to(destination).as_posix(),
        'category': category,
        'source': str(source),
        'size': target.stat().st_size,
        'sha256': sha256_of(target),
        'note': note,
    })
    return target


def collect_python_runtime(destination: Path, records: list[dict]) -> dict:
    prefix = Path(sys.base_prefix)
    candidates = [prefix / 'LICENSE.txt', prefix / 'LICENSE', prefix / 'LICENSE.rst']
    source = next((item for item in candidates if item.is_file()), None)
    entry = {'version': sys.version.split()[0], 'prefix': str(prefix),
             'candidate_paths': [str(item) for item in candidates], 'source': None,
             'sha256': None, 'status': 'MISSING'}
    if source is not None:
        copied = copy_file(source, destination, records, category='python-runtime',
                           relative=Path('python-runtime') / source.name)
        entry.update({'source': str(source), 'sha256': sha256_of(copied), 'status': 'COLLECTED'})
    return entry


def collect_packages(destination: Path, distributions: list[dict], records: list[dict]) -> None:
    for entry in distributions:
        folder = Path(entry['name'])
        folder.mkdir(parents=True, exist_ok=True)
        expression = entry['license_metadata']
        text = (f"{entry['name']} {entry['version']}\n"
                f"License: {expression if expression else 'UNRESOLVED (package metadata declares no license)'}\n")
        if entry['license_classifiers']:
            text += 'Classifiers: ' + '; '.join(entry['license_classifiers']) + '\n'
        target = destination / folder / 'package.txt'
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding='utf-8')
        records.append({'path': target.relative_to(destination).as_posix(),
                        'category': f"package-{entry['category']}",
                        'source': f"metadata:{entry['name']}", 'size': target.stat().st_size,
                        'sha256': sha256_of(target), 'note': None})
        for index, file_entry in enumerate(entry['license_files']):
            source = Path(file_entry['source'])
            if source.stat().st_size > MAX_NOTICE_BYTES:
                continue
            flattened = flattened_notice_name(Path(file_entry['relative']), index)
            copy_file(source, destination, records, category=f"package-{entry['category']}",
                      relative=folder / flattened)


def scan_archives(root: Path) -> list[dict]:
    """Archive-like files under a scanned tree, classified by packaging meaning."""
    found = []
    if not root.exists():
        return found
    for path in sorted(root.rglob('*')):
        if not path.is_file() or path.suffix.lower() not in ARCHIVE_SUFFIXES:
            continue
        kind = 'installer-input' if path.suffix.lower() in INSTALLER_INPUT_SUFFIXES else 'runtime-archive'
        found.append({'path': str(path), 'size': path.stat().st_size, 'kind': kind,
                      'relative': path.relative_to(root).as_posix()})
    return found


def collect_ocr_notices(app_dir: Path, destination: Path, records: list[dict]) -> dict:
    source_dir = app_dir / OCR_NOTICES_DIR
    result = {'source': str(source_dir), 'exists': source_dir.is_dir(), 'files': 0,
              'manifest': None, 'unresolved': [], 'missing_notices': []}
    if not source_dir.is_dir():
        return result
    for path in sorted(source_dir.rglob('*')):
        if not path.is_file():
            continue
        copy_file(path, destination, records, category='ocr-notice',
                  relative=Path('ocr') / path.relative_to(source_dir))
        result['files'] += 1
    manifest_path = source_dir / 'MANIFEST.json'
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        result['manifest'] = str(manifest_path)
        result['manifest_sha256'] = sha256_of(manifest_path)
        result['unresolved'] = [item['id'] for item in manifest.get('unresolved', [])]
        for entry in manifest.get('notices', []):
            if entry.get('status') != 'OK':
                result['missing_notices'].append(f"{entry['path']}: {entry['status']}")
    return result


def collect_office_notices(app_dir: Path, destination: Path, records: list[dict]) -> dict:
    office_root = app_dir / 'tools' / 'office'
    result = {'source': str(office_root), 'exists': office_root.is_dir(),
              'root_notices': [], 'missing_root_notices': [], 'indexed': 0, 'index': []}
    if not office_root.is_dir():
        return result
    for name in OFFICE_ROOT_NOTICES:
        source = office_root / name
        if source.is_file():
            copy_file(source, destination, records, category='office-root-notice',
                      relative=Path('office') / name)
            result['root_notices'].append({'name': name, 'sha256': sha256_of(source),
                                           'size': source.stat().st_size})
        else:
            result['missing_root_notices'].append(name)
    for path in sorted(office_root.rglob('*')):
        if not path.is_file() or not is_attribution_name(path.name):
            continue
        if path.suffix.lower() in ARCHIVE_SUFFIXES:
            continue
        result['indexed'] += 1
        result['index'].append({'path': path.relative_to(office_root).as_posix(),
                                'size': path.stat().st_size, 'sha256': sha256_of(path)})
    return result


def collect(destination: Path, app_dir: Path, *, platform_name: str | None = None) -> dict:
    destination.mkdir(parents=True, exist_ok=True)
    pre_existing = {path.relative_to(destination).as_posix() for path in destination.rglob('*')
                    if path.is_file() and path.name != REPORT_NAME}
    records: list[dict] = []
    declared_windows = parse_requirements(app_dir / 'requirements.txt')
    declared_macos = parse_requirements(app_dir / 'requirements-macos.txt')
    installed, duplicate_locations = environment_distributions()
    windows_closure, windows_unpinned = runtime_closure(declared_windows, installed)
    macos_closure, _ = runtime_closure(declared_macos, installed)
    is_macos = str(platform_name or sys.platform).lower() in {'darwin', 'mac', 'macos'}
    declared_active = declared_macos if is_macos else declared_windows
    closure, unpinned = (runtime_closure(declared_active, installed) if is_macos
                        else (windows_closure, windows_unpinned))
    requirement_file = 'requirements-macos.txt' if is_macos else 'requirements.txt'
    build_only_names = sorted({'pyinstaller', 'pyinstaller-hooks-contrib', 'altgraph',
                               'packaging', 'pefile', 'pywin32-ctypes', 'setuptools'}
                              & set(installed) - set(closure))

    declared_display = {normalize(item['name']): item['name']
                        for item in declared_windows + declared_macos}
    runtime_entries = [distribution_entry(installed[name], category='runtime',
                                          display_name=declared_display.get(name))
                       for name in closure if name in installed]
    build_entries = [distribution_entry(installed[name], category='build-only',
                                        display_name=declared_display.get(name))
                     for name in build_only_names]
    collect_packages(destination, runtime_entries + build_entries, records)
    runtime_license = collect_python_runtime(destination, records)
    ocr = collect_ocr_notices(app_dir, destination, records)
    office = collect_office_notices(app_dir, destination, records)

    flags: list[str] = []
    notes: list[str] = []
    if runtime_license['status'] != 'COLLECTED':
        flags.append('PYTHON_RUNTIME_LICENSE_MISSING: no LICENSE file found in ' + sys.base_prefix)
    for entry in runtime_entries + build_entries:
        label = f"{entry['name']} {entry['version']}"
        if not entry['license_files']:
            flags.append(f'PACKAGE_LICENSE_FILE_ABSENT: {label} ships no notice file in its metadata')
        if not entry['license_metadata'] and not entry['license_classifiers']:
            flags.append(f'PACKAGE_LICENSE_METADATA_EMPTY: {label} declares no license metadata')
    for name in unpinned:
        flags.append(f'TRANSITIVE_DEPENDENCY_NOT_PINNED: {name} is required by an installed '
                     f'dependency but absent from app/{requirement_file}')
    for name, locations in sorted(duplicate_locations.items()):
        notes.append(f'DUPLICATE_DISTRIBUTION: {name} is installed in {len(locations)} locations; '
                     f'collected from {installed[name]._path}')
    for requirement in declared_active:
        if normalize(requirement['name']) not in installed and normalize(requirement['name']) not in closure:
            flags.append(f'DECLARED_DEPENDENCY_NOT_INSTALLED: {requirement["name"]}')
    if not ocr['exists']:
        flags.append(f'OCR_NOTICE_INVENTORY_MISSING: {ocr["source"]} does not exist')
    for item in ocr['missing_notices']:
        flags.append(f'OCR_NOTICE_NOT_VERIFIED: {item}')
    for component in ocr['unresolved']:
        flags.append(f'OCR_COMPONENT_UNRESOLVED: {component} has no verified upstream notice')
    if not office['exists']:
        flags.append(f'OFFICE_TREE_MISSING: {office["source"]} does not exist')
    for name in office['missing_root_notices']:
        flags.append(f'OFFICE_ROOT_NOTICE_MISSING: {name}')
    staging_archives = scan_archives(app_dir / 'tools')
    for archive in staging_archives:
        if archive['kind'] == 'installer-input':
            flags.append(f'INSTALLER_INPUT_IN_STAGING: {archive["path"]} '
                         f"({archive['size']} bytes) is present in a staged tool tree and must "
                         'never reach a release bundle')
    if ocr['exists'] and ocr['manifest'] is None:
        flags.append('OCR_NOTICE_MANIFEST_MISSING: '
                     f'{ocr["source"]}/MANIFEST.json has not been generated yet')
    report = {
        'schema': 1,
        'destination': str(destination),
        'interpreter': {'executable': sys.executable, 'version': sys.version.split()[0],
                        'base_prefix': sys.base_prefix},
        'closure_basis': ('declared requirements plus installed Requires-Dist entries; '
                          'extra-gated requirements are skipped'),
        'declared': {'windows': declared_windows, 'macos': declared_macos},
        'target_platform': 'macos' if is_macos else 'windows',
        'runtime_closure': closure,
        'windows_runtime_closure': windows_closure,
        'macos_runtime_closure': macos_closure,
        'unpinned_transitive': unpinned,
        'duplicate_distributions': duplicate_locations,
        'python_runtime': runtime_license,
        'packages': runtime_entries + build_entries,
        'ocr': {key: value for key, value in ocr.items() if key != 'index'},
        'office': office,
        'staging_archives': staging_archives,
        'files': sorted(records, key=lambda item: item['path']),
        'file_count': len(records),
        'unmanaged_files': sorted(pre_existing - {entry['path'] for entry in records}),
        'flags': flags,
        'notes': sorted(notes),
    }
    (destination / REPORT_NAME).write_text(
        json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + '\n', encoding='utf-8')
    return report


def check(destination: Path) -> list[dict]:
    """Verify every collected file against the recorded report (copy fidelity)."""
    problems = []
    report_path = destination / REPORT_NAME
    if not report_path.is_file():
        return [{'path': REPORT_NAME, 'status': 'MISSING',
                 'detail': 'no collection report; run the collector first'}]
    for entry in json.loads(report_path.read_text(encoding='utf-8'))['files']:
        target = destination / entry['path']
        if not target.is_file():
            problems.append({'path': entry['path'], 'status': 'MISSING', 'detail': None})
            continue
        actual = sha256_of(target)
        if actual != entry['sha256']:
            problems.append({'path': entry['path'], 'status': 'CHANGED',
                             'detail': f"recorded {entry['sha256'][:12]}…, found {actual[:12]}…"})
    for path in sorted(destination.rglob('*')):
        if not path.is_file() or path.name == REPORT_NAME:
            continue
        if path.suffix.lower() in ARCHIVE_SUFFIXES:
            problems.append({'path': path.relative_to(destination).as_posix(),
                             'status': 'ARCHIVE_IN_DESTINATION',
                             'detail': 'installer inputs must never be collected'})
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', nargs='?', type=Path, default=DEFAULT_DESTINATION)
    parser.add_argument('--app-dir', type=Path, default=PROJECT_ROOT / 'app')
    parser.add_argument('--check', action='store_true',
                        help='verify an existing collection instead of writing it')
    parser.add_argument('--strict', action='store_true', help='exit non-zero when flags are present')
    parser.add_argument('--print-flags', action='store_true', help='print every flag')
    parser.add_argument('--json', action='store_true', help='print the report as JSON')
    args = parser.parse_args(argv)

    destination = args.destination.resolve()
    app_dir = args.app_dir.resolve()
    if args.check:
        problems = check(destination)
        print(f'check: {len(problems)} problem(s) in {destination}')
        for item in problems[:10]:
            print(f"  {item['status']}: {item['path']} {item.get('detail') or ''}")
        return 1 if problems else 0

    report = collect(destination, app_dir)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False))
    print(f"collected {report['file_count']} files into {destination}")
    print(f"runtime packages: {len(report['runtime_closure'])}, "
          f"build-only: {len([p for p in report['packages'] if p['category'] == 'build-only'])}")
    print(f"flags: {len(report['flags'])}")
    if args.print_flags:
        for flag in report['flags']:
            print(f'  {flag}')
    else:
        for flag in report['flags'][:10]:
            print(f'  {flag}')
    return 1 if (args.strict and report['flags']) else 0


if __name__ == '__main__':
    raise SystemExit(main())
