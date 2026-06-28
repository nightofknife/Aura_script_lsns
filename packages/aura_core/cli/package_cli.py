# -*- coding: utf-8 -*-
"""CLI utilities for package manifests."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from packages.aura_core.packaging.manifest.generator import ManifestGenerator
from packages.aura_core.packaging.manifest.parser import ManifestParser


def cmd_init(args):
    path = Path(args.package_path)
    path.mkdir(parents=True, exist_ok=True)
    (path / "src").mkdir(exist_ok=True)
    (path / "src" / "services").mkdir(exist_ok=True)
    (path / "src" / "actions").mkdir(exist_ok=True)
    (path / "tasks").mkdir(exist_ok=True)
    (path / "config").mkdir(exist_ok=True)

    generator = ManifestGenerator(path)
    manifest_data = generator._create_default_manifest()
    generator.save(manifest_data)
    print(f"[OK] Package initialized: {path}")


def cmd_build(args):
    path = Path(args.package_path)
    generator = ManifestGenerator(path)
    manifest_data = generator.generate(preserve_manual_edits=not args.force)
    generator.save(manifest_data)
    print(f"[OK] Manifest generated: {path / 'manifest.yaml'}")


def cmd_sync(args):
    path = Path(args.package_path)
    generator = ManifestGenerator(path)
    manifest_data = generator.generate(preserve_manual_edits=True)
    generator.save(manifest_data)
    print(f"[OK] Manifest synced: {path / 'manifest.yaml'}")


def cmd_check(args):
    path = Path(args.package_path)
    manifest_path = path / "manifest.yaml"
    if not manifest_path.exists():
        print("[ERROR] manifest.yaml not found", file=sys.stderr)
        raise SystemExit(1)

    generator = ManifestGenerator(path)
    expected = generator.generate(preserve_manual_edits=True)
    current = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    if _normalize_manifest_for_check(current) == _normalize_manifest_for_check(expected):
        print("[OK] Manifest is up to date")
        return

    print("[ERROR] Manifest is out of date. Run `aura manifest sync`.", file=sys.stderr)
    raise SystemExit(1)


def _normalize_manifest_for_check(data):
    if not isinstance(data, dict):
        return data
    normalized = dict(data)
    metadata = dict(normalized.get("metadata", {}) or {})
    metadata.pop("generated_at", None)
    normalized["metadata"] = metadata
    return normalized


def cmd_validate(args):
    path = Path(args.package_path)
    manifest_path = path / "manifest.yaml"
    if not manifest_path.exists():
        print("[ERROR] manifest.yaml not found", file=sys.stderr)
        raise SystemExit(1)

    manifest = ManifestParser.parse(manifest_path)
    errors = ManifestParser.validate(manifest)
    if errors:
        print("[ERROR] Manifest validation failed:")
        for error in errors:
            print(f"  - {error}")
        raise SystemExit(1)
    print("[OK] Manifest validation passed")


def cmd_info(args):
    path = Path(args.package_path)
    manifest_path = path / "manifest.yaml"
    if not manifest_path.exists():
        print("[ERROR] manifest.yaml not found", file=sys.stderr)
        raise SystemExit(1)

    manifest = ManifestParser.parse(manifest_path)
    print(f"Package: {manifest.package.name}")
    print(f"Version: {manifest.package.version}")
    print(f"Description: {manifest.package.description}")
    print("\nExports:")
    print(f"  - Services: {len(manifest.exports.services)}")
    print(f"  - Actions: {len(manifest.exports.actions)}")
    print(f"  - Tasks: {len(manifest.exports.tasks)}")
    print("\nDependencies:")
    for dep_name, dep_spec in manifest.dependencies.items():
        print(f"  - {dep_name} ({dep_spec.version})")


def main():
    parser = argparse.ArgumentParser(description="Aura package manifest tool")
    subparsers = parser.add_subparsers(dest="command", help="Commands")

    parser_init = subparsers.add_parser("init", help="Initialize a new package")
    parser_init.add_argument("package_path", help="Package path")
    parser_init.set_defaults(func=cmd_init)

    parser_build = subparsers.add_parser("build", help="Generate manifest.yaml from code")
    parser_build.add_argument("package_path", help="Package path")
    parser_build.add_argument("--force", action="store_true", help="Overwrite generated sections")
    parser_build.set_defaults(func=cmd_build)

    parser_sync = subparsers.add_parser("sync", help="Sync manifest generated sections")
    parser_sync.add_argument("package_path", help="Package path")
    parser_sync.set_defaults(func=cmd_sync)

    parser_check = subparsers.add_parser("check", help="Check whether manifest is up to date")
    parser_check.add_argument("package_path", help="Package path")
    parser_check.set_defaults(func=cmd_check)

    parser_validate = subparsers.add_parser("validate", help="Validate manifest.yaml")
    parser_validate.add_argument("package_path", help="Package path")
    parser_validate.set_defaults(func=cmd_validate)

    parser_info = subparsers.add_parser("info", help="Show package info")
    parser_info.add_argument("package_path", help="Package path")
    parser_info.set_defaults(func=cmd_info)

    args = parser.parse_args()
    if hasattr(args, "func"):
        args.func(args)
        return
    parser.print_help()


if __name__ == "__main__":
    main()
