"""Portable normalized archives use the public schema validator and bounded transport."""

import json
import stat
import zipfile

import agent_trajviz as tv
import pytest
from agent_trajviz import LoadError

from trajviz_obs import archive
from trajviz_obs.bundle_cmd import bundle_session
from trajviz_obs.compat import load_run


def test_normalized_archive_preserves_canonical_steps_and_provenance(fixture_run, tmp_path):
    path = tmp_path / "portable.zip"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as output:
        output.writestr("trajectory.json", json.dumps(fixture_run.to_dict(include_raw=True)))
    restored = load_run(str(path))
    assert restored.to_dict(include_raw=True) == fixture_run.to_dict(include_raw=True)
    with pytest.raises(LoadError) as caught:
        tv.load(path, format="pi")
    assert caught.value.code == "mismatch"


@pytest.mark.parametrize("unsafe", ["symlink", "size"])
def test_normalized_archive_rejects_symlinks_and_oversized_members(fixture_run, tmp_path, monkeypatch, unsafe):
    path = tmp_path / "unsafe.zip"
    member = zipfile.ZipInfo("trajectory.json")
    if unsafe == "symlink":
        member.create_system = 3
        member.external_attr = (stat.S_IFLNK | 0o777) << 16
    else:
        monkeypatch.setattr(archive, "MAX_BYTES", 4)
    with zipfile.ZipFile(path, "w") as output:
        output.writestr(member, json.dumps(fixture_run.to_dict(include_raw=True)))
    with pytest.raises(LoadError) as caught:
        load_run(str(path))
    assert caught.value.code == ("unsafe_bundle" if unsafe == "symlink" else "size_limit")


def test_normalized_archive_still_validates_export_schema(fixture_run, tmp_path):
    path = tmp_path / "future.zip"
    document = fixture_run.to_dict(include_raw=True)
    document["schema_version"] = 999
    with zipfile.ZipFile(path, "w") as output:
        output.writestr("trajectory.json", json.dumps(document))
    with pytest.raises(LoadError) as caught:
        load_run(str(path))
    assert caught.value.code == "unsupported_schema"


def test_normalized_json_bundle_reloads_even_when_renamed(fixture_run, tmp_path):
    source = tmp_path / "renamed.json"
    source.write_text(json.dumps(fixture_run.to_dict(include_raw=True)))
    result = bundle_session(str(source), tmp_path / "bundle.zip")
    assert result["members"] == ["trajectory.json"]
    assert load_run(result["path"]).steps == fixture_run.steps
