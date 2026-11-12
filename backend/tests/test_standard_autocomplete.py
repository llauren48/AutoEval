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
        assert template["params"]
        assert digest(template) == digest(autocomplete_template(entry))
        result = compile_config(entry, CompileInput(template=template))
        assert "startup_template_required" not in result["blocking_reasons"]
        assert "model_mapping_missing" in result["blocking_reasons"]
        assert "tool_root_missing" in result["blocking_reasons"]
        assert not result["input_ready"] and not result["execution_enabled"]
        assert result["approval_status"] == "not_approved"
        assert entry == original
        assert "api_key" not in str(result)
