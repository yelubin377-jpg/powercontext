#!/usr/bin/env python3
# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Publish a distributable Skill to the ModelScope community.

The script validates the Skill package, appends the team verification line to the
end of ``SKILL.md``, packs the package into the archive that ModelScope expects,
and publishes it as a public Skill. It never submits anything to the competition
platform: the work link it prints is the input for that separate step.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PACKAGE_ROOT = REPOSITORY_ROOT / "skill-packages"
DEFAULT_CATEGORY = "developer-tools"
DEFAULT_LICENSE = "Apache-2.0"
MAX_ARCHIVE_BYTES = 5 * 1024 * 1024
REQUIRED_FIELDS = ("name", "version", "description")
SKILL_NAME_CHARACTERS = set("abcdefghijklmnopqrstuvwxyz0123456789-")


class SkillPackageError(RuntimeError):
    """The Skill package cannot be published as it is."""


@dataclass(frozen=True, slots=True)
class SkillPackage:
    """One validated Skill package on disk."""

    path: Path
    fields: dict[str, str]

    @property
    def name(self) -> str:
        return self.fields["name"]

    @property
    def version(self) -> str:
        return self.fields["version"]

    @property
    def description(self) -> str:
        return self.fields["description"]


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Split a SKILL.md into its YAML front matter fields and its body."""

    if not text.startswith("---\n"):
        raise SkillPackageError("SKILL.md must start with a YAML front matter block")  # noqa: TRY003
    end = text.find("\n---\n", 3)
    if end == -1:
        raise SkillPackageError("SKILL.md front matter block is not closed")  # noqa: TRY003
    fields: dict[str, str] = {}
    for line in text[4:end].splitlines():
        if not line.strip():
            continue
        if line[:1].isspace():
            raise SkillPackageError("SKILL.md front matter must use one line per field")  # noqa: TRY003
        key, separator, value = line.partition(":")
        if not separator or not key.strip():
            raise SkillPackageError(f"SKILL.md front matter line is not a field: {line!r}")  # noqa: TRY003
        fields[key.strip()] = _unquote(value.strip())
    return fields, text[end + len("\n---\n") :]


def _unquote(value: str) -> str:
    """Return one front matter value without its optional surrounding quotes."""

    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def validate_package(package: Path) -> SkillPackage:
    """Validate one Skill package against the ModelScope Skill requirements."""

    if not package.is_dir():
        raise SkillPackageError(f"the Skill package directory {package} does not exist")  # noqa: TRY003
    skill_file = package / "SKILL.md"
    if not skill_file.is_file():
        raise SkillPackageError(f"{skill_file} is missing")  # noqa: TRY003
    fields, _ = parse_frontmatter(skill_file.read_text(encoding="utf-8"))
    missing = [field for field in REQUIRED_FIELDS if not fields.get(field)]
    if missing:
        raise SkillPackageError(f"SKILL.md front matter is missing {', '.join(missing)}")  # noqa: TRY003
    name = fields["name"]
    if name != package.name:
        raise SkillPackageError(  # noqa: TRY003
            f"the SKILL.md name {name!r} must match the package directory name {package.name!r}"
        )
    if name.startswith("-") or name.endswith("-") or not set(name) <= SKILL_NAME_CHARACTERS:
        raise SkillPackageError(  # noqa: TRY003
            "the SKILL.md name must use lowercase letters, digits, and single hyphens only"
        )
    duplicates = sorted(path.relative_to(package).as_posix() for path in package.rglob("SKILL.md"))
    if len(duplicates) > 1:
        raise SkillPackageError(f"the package must contain exactly one SKILL.md, found {duplicates}")  # noqa: TRY003
    return SkillPackage(path=package, fields=fields)


def append_verification_line(text: str, verification_line: str) -> str:
    """Append the team verification line to the end of the SKILL.md body."""

    line = verification_line.strip()
    if not line:
        raise SkillPackageError("the team verification line must not be empty")  # noqa: TRY003
    if line in text:
        return text
    return f"{text.rstrip()}\n\n{line}\n"


def build_skill_archive(
    package: SkillPackage,
    verification_line: str | None,
    destination: Path,
) -> Path:
    """Pack the Skill package into the archive that ModelScope accepts."""

    with tempfile.TemporaryDirectory() as workspace:
        staging = Path(workspace) / package.name
        shutil.copytree(package.path, staging)
        if verification_line is not None:
            skill_file = staging / "SKILL.md"
            skill_file.write_text(
                append_verification_line(skill_file.read_text(encoding="utf-8"), verification_line),
                encoding="utf-8",
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(staging.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(staging).as_posix())
    size = destination.stat().st_size
    if size > MAX_ARCHIVE_BYTES:
        raise SkillPackageError(f"the Skill archive is {size} bytes, above the {MAX_ARCHIVE_BYTES} byte limit")  # noqa: TRY003
    return destination


def publish_skill(
    *,
    owner: str | None,
    package: SkillPackage,
    archive: Path,
    category: str,
    visibility: str,
    license_name: str,
    description: str,
    token: str | None,
) -> tuple[str, str]:
    """Create or update the public Skill and return the state and the owner."""

    try:
        from modelscope_hub.api import HubApi
    except ImportError:  # pragma: no cover - depends on the installed SDK
        from modelscope.hub.api import HubApi

    api = HubApi(token=token)
    if not owner:
        owner = api.openapi.get_current_username()
        if not owner:
            raise SkillPackageError("the ModelScope account handle is unknown; pass --owner")  # noqa: TRY003
    file_id = api.upload_file_to_openapi(archive)
    repo_id = f"{owner}/{package.name}"
    settings = {
        "skill_file": file_id,
        "description": description,
        "license": license_name,
        "category": category,
        "private": visibility == "private",
    }
    create_error: Exception | None = None
    try:
        api.create_repo(
            repo_id,
            "skill",
            visibility=visibility,
            license=license_name,
            description=description,
            chinese_name=package.name,
            category=category,
            skill_file=file_id,
        )
    except Exception as error:
        create_error = error
    if create_error is None:
        return "created", owner
    try:
        api.openapi.update_skill_settings(owner, package.name, settings)
    except Exception:
        raise create_error from None
    return "updated", owner


def resolve_verification_line(arguments: argparse.Namespace) -> str | None:
    """Return the team verification line from the command line or a file."""

    if arguments.verification_line and arguments.verification_line_file:
        raise SkillPackageError("use either --verification-line or --verification-line-file, not both")  # noqa: TRY003
    if arguments.verification_line_file:
        path = Path(arguments.verification_line_file).expanduser()
        if not path.is_file():
            raise SkillPackageError(f"the verification line file {path} does not exist")  # noqa: TRY003
        return path.read_text(encoding="utf-8").strip()
    return arguments.verification_line


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--owner", help="the ModelScope account that owns the Skill; resolved from the token when omitted"
    )
    parser.add_argument("--name", help="the Skill name; defaults to the package directory name")
    parser.add_argument(
        "--package-root", default=str(DEFAULT_PACKAGE_ROOT), help="the directory holding the Skill packages"
    )
    parser.add_argument("--package", help="the Skill package directory; defaults to <package-root>/<name>")
    parser.add_argument("--verification-line", help="the team verification line appended to the end of SKILL.md")
    parser.add_argument("--verification-line-file", help="a file that holds the team verification line")
    parser.add_argument("--category", default=DEFAULT_CATEGORY, help="the ModelScope Skill category")
    parser.add_argument("--visibility", default="public", choices=("public", "private"))
    parser.add_argument("--license", default=DEFAULT_LICENSE, help="the SPDX license of the Skill")
    parser.add_argument("--description", help="the Skill description shown on the ModelScope page")
    parser.add_argument("--token", default=os.environ.get("MODELSCOPE_API_TOKEN"), help="the ModelScope API token")
    parser.add_argument("--archive", help="write the Skill archive to this path")
    parser.add_argument("--dry-run", action="store_true", help="validate and pack the Skill without publishing it")
    return parser


def resolve_package(arguments: argparse.Namespace) -> Path:
    """Return the Skill package directory selected on the command line."""

    if arguments.package:
        return Path(arguments.package).expanduser()
    if not arguments.name:
        raise SkillPackageError("pass --name or --package to select the Skill package")  # noqa: TRY003
    return Path(arguments.package_root).expanduser() / arguments.name


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        package = validate_package(resolve_package(arguments))
        verification_line = resolve_verification_line(arguments)
        with tempfile.TemporaryDirectory() as workspace:
            archive = Path(workspace) / f"{package.name}.zip"
            build_skill_archive(package, verification_line, archive)
            if arguments.archive:
                shutil.copyfile(archive, Path(arguments.archive).expanduser())
            if arguments.dry_run:
                print(f"The Skill package {package.name} is valid.")
                print(f"The archive contains {len(zipfile.ZipFile(archive).namelist())} files and is ready to publish.")
                if verification_line:
                    print("The team verification line is appended to the end of SKILL.md.")
                return 0
            state, owner = publish_skill(
                owner=arguments.owner,
                package=package,
                archive=archive,
                category=arguments.category,
                visibility=arguments.visibility,
                license_name=arguments.license,
                description=arguments.description or package.description,
                token=arguments.token,
            )
    except SkillPackageError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(f"Skill {state}: https://modelscope.cn/skills/@{owner}/{package.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
