"""Input-only completion preserves constraints and never invents environment values."""

import copy
from pathlib import Path

import pytest

from autoeval.standard_inputs import (
    Bundle,
    CompileInput,
    autocomplete_template,
    compile_config,
    digest,
    discover,
)


@pytest.mark.parametrize("filename", ["upstream.json", "minimax.json"])
def test_completion_uses_pinned_constraints_and_preserves_original(filename):
    bundle = Bundle.model_validate_json(
        (Path(__file__).parent / "fixtures/standard_inputs" / filename).read_bytes()
    )
    for entry in discover(bundle):
        original = copy.deepcopy(entry)
        template = autocomplete_template(entry)
        assert template == {}
        assert digest(template) == digest(autocomplete_template(entry))
        result = compile_config(entry, CompileInput(template=template))
        assert "startup_template_required" in result["blocking_reasons"]
        assert "model_mapping_missing" in result["blocking_reasons"]
        assert "tool_root_missing" in result["blocking_reasons"]
        assert not result["input_ready"] and not result["execution_enabled"]
        assert result["approval_status"] == "not_required"
        assert entry == original
        assert "api_key" not in str(result)


def test_repository_readiness_does_not_require_runtime_or_accept_fabricated_template():
    from autoeval.standard_inputs import input_readiness

    bundle = Bundle.model_validate_json(
        (Path(__file__).parent / "fixtures/standard_inputs/upstream.json").read_bytes()
    )
    entry = discover(bundle)[0]
    result = compile_config(
        entry, CompileInput(input_policy="repository", template={"params": {"max_num_workers": 1}})
    )
    assert "startup_template_required" in result["blocking_reasons"]
    assert input_readiness(entry)["standard_config_status"] == "missing"
    entry["files"]["benchmark_config.json"] = entry["files"]["standard_config.json"]
    assert input_readiness(entry)["standard_config_status"] == "complete"
    assert input_readiness(entry)["environment_status"] == "not_verified"
