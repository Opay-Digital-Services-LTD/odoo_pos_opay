#!/usr/bin/env python3
"""Build a clean GitHub release ZIP for the pos_opay addon."""

import argparse
import ast
import hashlib
from pathlib import Path, PurePosixPath
import subprocess
import zipfile


ADDON_NAME = "pos_opay"
EXCLUDED_FILES = {
    ".gitignore",
    "AGENTS.md",
    "docs/PRD.md",
}
EXCLUDED_PREFIXES = (
    ".github/",
    "dist/",
)


def tracked_files(repository):
    output = subprocess.check_output(
        ["git", "-C", str(repository), "ls-files", "-z"]
    )
    for raw_path in output.decode("utf-8").split("\0"):
        if not raw_path:
            continue
        path = PurePosixPath(raw_path).as_posix()
        if path in EXCLUDED_FILES or path.startswith(EXCLUDED_PREFIXES):
            continue
        if "__pycache__" in PurePosixPath(path).parts or path.endswith(".pyc"):
            continue
        yield path


def read_manifest(repository):
    manifest_path = repository / "__manifest__.py"
    manifest = ast.literal_eval(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise SystemExit("__manifest__.py must contain one dictionary.")
    return manifest


def build_release(repository, tag, output_directory):
    manifest = read_manifest(repository)
    version = manifest.get("version")
    if version != tag:
        raise SystemExit(
            f"Release tag {tag!r} does not match manifest version {version!r}."
        )
    if not tag.startswith(("18.0.", "19.0.")):
        raise SystemExit("Only Odoo 18.0 and 19.0 release tags are supported.")

    files = sorted(set(tracked_files(repository)))
    required_files = {
        "__init__.py",
        "__manifest__.py",
        "LICENSE",
        "README.md",
        "INSTALLATION.md",
        "static/description/icon.png",
        "static/description/index.html",
        *manifest.get("images", []),
    }
    missing = sorted(required_files.difference(files))
    if missing:
        raise SystemExit(
            "Required release files are missing or untracked: " + ", ".join(missing)
        )

    output_directory.mkdir(parents=True, exist_ok=True)
    archive_path = output_directory / f"{ADDON_NAME}-{version}.zip"
    with zipfile.ZipFile(
        archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as archive:
        for relative_path in files:
            source_path = repository / relative_path
            if not source_path.is_file():
                raise SystemExit(f"Tracked release file is missing: {relative_path}")
            archive.write(source_path, f"{ADDON_NAME}/{relative_path}")

    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    checksum_path = archive_path.with_suffix(archive_path.suffix + ".sha256")
    checksum_path.write_text(
        f"{digest}  {archive_path.name}\n",
        encoding="ascii",
    )
    print(archive_path)
    print(checksum_path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", required=True)
    parser.add_argument("--output-directory", default="dist")
    arguments = parser.parse_args()
    repository = Path(__file__).resolve().parents[2]
    build_release(
        repository,
        arguments.tag,
        repository / arguments.output_directory,
    )


if __name__ == "__main__":
    main()
