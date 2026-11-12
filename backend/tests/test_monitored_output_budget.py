import pytest

from autoeval.monitored_runtime import validate_budget


def request(output=2048, cap=2048):
    return {
        "budget": {"max_model_requests": 5, "max_output_tokens": cap},
        "config": {
            "params": {
                "num_prompts": 5,
                "max_out_len": output,
                "retry": 1,
                "batch_size": 1,
                "max_num_workers": 1,
            }
        },
    }


def test_larger_output_is_explicit_and_does_not_relax_old_snapshot():
    validate_budget(request())
    validate_budget(request(64, 64))
    with pytest.raises(ValueError):
        validate_budget(request(2048, 64))
    with pytest.raises(ValueError):
        validate_budget(request(2049, 2048))
    with pytest.raises(ValueError):
        validate_budget(request(4096, 4096))
