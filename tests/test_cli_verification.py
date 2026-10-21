"""Portable verification must be independent of the original store and model account."""

import json
from pathlib import Path

import pytest

from orchestrator import cli
from orchestrator.artifacts import manifest


def test_read_only_verification_does_not_open_store_or_provider(tmp_path, monkeypatch, capsys):
    from orchestrator import bundle
    monkeypatch.setattr(bundle, "verify_bundle", lambda path: {"accepted": [], "notice": "Historical evidence only"})
    monkeypatch.setattr(cli, "Store", lambda *args: pytest.fail("Verification must not create a run store"))
    monkeypatch.setattr(cli, "LocalProvider", lambda: pytest.fail("Verification must not construct a model provider"))
    run_dir = tmp_path / "unused-store"
    assert cli.main(["--runs-dir", str(run_dir), "verify-bundle", str(tmp_path)]) == 0
    assert not run_dir.exists()
    assert json.loads(capsys.readouterr().out)["notice"] == "Historical evidence only"


@pytest.mark.parametrize("mode", ["local"])
def test_isolated_candidate_rerun_uses_docker_without_a_model_key(tmp_path, monkeypatch, capsys, mode):
    from orchestrator import bundle
    candidate = tmp_path / "bundle" / "candidate"
    (candidate / "service" / "shortener").mkdir(parents=True)
    (candidate / "service" / "shortener" / "example.py").write_text("# Not executed by this runner double.\n")
    checked = {"accepted": [{"run_id": "sample", "mode": mode, "scenario": "greenfield",
                              "alias_min": 4, "alias_max": 32, "candidate_path": str(candidate),
                              "candidate_hash": manifest(candidate)["hash"]}]}
    monkeypatch.setattr(bundle, "verify_bundle", lambda path: checked)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(cli, "LocalProvider", lambda: pytest.fail("Rerun must not use inference"))
    observed = []
    constructed_modes = []

    class DockerDouble:
        def __init__(self, image, *, mode="local"):
            constructed_modes.append(mode)

        def preflight(self):
            observed.append("preflight")

        def validate(self, source, output, scenario, minimum, maximum):
            observed.append("validate")
            output.mkdir()
            return {"passed": True, "tests": 10, "report_path": str(output / "report.json"), "summary": "Explicit double"}

    monkeypatch.setattr(cli, "DockerRunner", DockerDouble)
    output = tmp_path / "fresh-reports"
    assert cli.main(["verify-bundle", str(candidate.parent), "--rerun", "--output", str(output)]) == 0
    assert observed == ["preflight", "validate"]
    assert constructed_modes[-1] == mode
    result = json.loads(capsys.readouterr().out)
    assert result["model_calls"] == 0
    assert (output / "summary.json").is_file()


def test_rerun_requires_new_output_outside_bundle(tmp_path, monkeypatch, capsys):
    from orchestrator import bundle
    monkeypatch.setattr(bundle, "verify_bundle", lambda path: {"accepted": []})
    assert cli.main(["verify-bundle", str(tmp_path), "--rerun", "--output", str(tmp_path / "reports")]) == 2
    assert "outside the evidence bundle" in capsys.readouterr().err
    assert not (tmp_path / "reports").exists()


def test_rerun_rejects_symlink_parent_before_creating_directories(tmp_path, monkeypatch, capsys):
    from orchestrator import bundle
    source = tmp_path / "bundle"
    source.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "alias"
    link.symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(bundle, "verify_bundle", lambda path: {"accepted": []})
    output = link / "new-parent" / "reports"
    assert cli.main(["verify-bundle", str(source), "--rerun", "--output", str(output)]) == 2
    assert "symlinks" in capsys.readouterr().err
    assert not (outside / "new-parent").exists()
