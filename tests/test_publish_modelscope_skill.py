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

from __future__ import annotations

import zipfile
from pathlib import Path

import publish_modelscope_skill as publisher
import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPOSITORY_ROOT / "skill-packages"
SHIPPED_PACKAGE = PACKAGE_ROOT / "powercontext-project-context"
VERIFICATION_LINE = "OB-VERIFY: competition-600075-team-42-token-abcdef"


def write_package(root: Path, name: str, frontmatter: str, body: str = "# Skill\n") -> Path:
    package = root / name
    package.mkdir(parents=True)
    (package / "SKILL.md").write_text(f"---\n{frontmatter}\n---\n\n{body}", encoding="utf-8")
    return package


def test_the_shipped_package_is_valid() -> None:
    package = publisher.validate_package(SHIPPED_PACKAGE)
    assert package.name == "powercontext-project-context"
    assert package.version
    assert package.description
    assert package.description == package.fields["description"]


def test_a_package_without_a_version_is_rejected(tmp_path: Path) -> None:
    package = write_package(
        tmp_path,
        "demo-skill",
        "name: demo-skill\ndescription: A demo Skill.",
    )
    with pytest.raises(publisher.SkillPackageError):
        publisher.validate_package(package)


def test_the_name_must_match_the_package_directory(tmp_path: Path) -> None:
    package = write_package(
        tmp_path,
        "demo-skill",
        "name: other-skill\nversion: 1.0.0\ndescription: A demo Skill.",
    )
    with pytest.raises(publisher.SkillPackageError):
        publisher.validate_package(package)


def test_the_name_rejects_unsupported_characters(tmp_path: Path) -> None:
    package = write_package(
        tmp_path,
        "demo-skill",
        "name: Demo_Skill\nversion: 1.0.0\ndescription: A demo Skill.",
    )
    with pytest.raises(publisher.SkillPackageError):
        publisher.validate_package(package)


def test_a_package_must_hold_exactly_one_skill_file(tmp_path: Path) -> None:
    package = write_package(
        tmp_path,
        "demo-skill",
        "name: demo-skill\nversion: 1.0.0\ndescription: A demo Skill.",
    )
    nested = package / "references"
    nested.mkdir()
    (nested / "SKILL.md").write_text("# nested\n", encoding="utf-8")
    with pytest.raises(publisher.SkillPackageError):
        publisher.validate_package(package)


def test_a_quoted_front_matter_value_is_unquoted(tmp_path: Path) -> None:
    package = write_package(
        tmp_path,
        "demo-skill",
        'name: demo-skill\nversion: "1.0.0"\ndescription: A demo Skill.',
    )
    validated = publisher.validate_package(package)
    assert validated.version == "1.0.0"


def test_the_verification_line_is_appended_once_at_the_end() -> None:
    text = "---\nname: demo\nversion: 1.0.0\ndescription: A demo Skill.\n---\n\n# Demo\n"
    appended = publisher.append_verification_line(text, VERIFICATION_LINE)
    assert appended.endswith(f"{VERIFICATION_LINE}\n")
    assert appended.count(VERIFICATION_LINE) == 1
    assert publisher.append_verification_line(appended, VERIFICATION_LINE) == appended


def test_an_empty_verification_line_is_rejected() -> None:
    with pytest.raises(publisher.SkillPackageError):
        publisher.append_verification_line("# Demo\n", "   ")


def test_the_archive_keeps_the_skill_file_at_the_root(tmp_path: Path) -> None:
    package = write_package(
        tmp_path,
        "demo-skill",
        "name: demo-skill\nversion: 1.0.0\ndescription: A demo Skill.",
    )
    references = package / "references"
    references.mkdir()
    (references / "scope.md").write_text("# Scope\n", encoding="utf-8")

    validated = publisher.validate_package(package)
    archive = publisher.build_skill_archive(validated, VERIFICATION_LINE, tmp_path / "demo-skill.zip")
    with zipfile.ZipFile(archive) as bundle:
        names = bundle.namelist()
        assert "SKILL.md" in names
        assert "references/scope.md" in names
        assert VERIFICATION_LINE in bundle.read("SKILL.md").decode("utf-8")


def test_the_archive_limit_is_enforced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    package = write_package(
        tmp_path,
        "demo-skill",
        "name: demo-skill\nversion: 1.0.0\ndescription: A demo Skill.",
    )
    validated = publisher.validate_package(package)
    monkeypatch.setattr(publisher, "MAX_ARCHIVE_BYTES", 1)
    with pytest.raises(publisher.SkillPackageError):
        publisher.build_skill_archive(validated, None, tmp_path / "demo-skill.zip")


def test_the_dry_run_needs_neither_an_owner_nor_a_token(capsys: pytest.CaptureFixture) -> None:
    exit_code = publisher.main(
        ["--name", "powercontext-project-context", "--package-root", str(PACKAGE_ROOT), "--dry-run"]
    )
    assert exit_code == 0
    assert "ready to publish" in capsys.readouterr().out
