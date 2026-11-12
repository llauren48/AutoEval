import pytest
from pydantic import ValidationError

from autoeval.configuration import Parameters


def values():
    return {"output": 32, "concurrency": 1, "temperature": 0, "top_p": 1, "scope": "Lite"}


def test_nested_custom_parameters_roundtrip_and_legacy_shape():
    assert Parameters(**values()).model_dump() == values()
    config = {"tool": "ais_bench", "subcommand": "llm_accuracy", "params": {
        "datasets": ["gpqa_gen_0_shot_cot_chat_prompt"],
        "generation_kwargs": {"reasoning_effort": "max", "temperature": 1.0},
        "max_out_len": 262144,
    }}
    saved = Parameters(**values(), adapter_input=config).model_dump()
    assert Parameters.model_validate(saved).model_dump()["adapter_input"] == config


@pytest.mark.parametrize("params", [{"api_key": "plaintext"}, {"unknown": True}])
def test_custom_adapter_rejects_credentials_and_unknown_fields(params):
    with pytest.raises(ValidationError):
        Parameters(**values(), adapter_input={"tool": "ais_bench", "subcommand": "llm_accuracy", "params": params})
