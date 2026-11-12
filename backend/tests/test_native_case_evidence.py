import hashlib
import json
import uuid

import pytest

from autoeval.native_case_evidence import native_content, prediction_records


def fixture():
    run_id = uuid.uuid4()
    row = {
        "id": 0,
        "data_abbr": "GPQA_diamond",
        "uuid": "native-uuid",
        "origin_prompt": [{"role": "HUMAN", "prompt": "Actual AISBench prompt"}],
        "prediction": "Actual response\nwith a line break",
    }
    content = json.dumps(row).replace("\\n", "\n")
    entry = {
        "path": "output/experiment/predictions/model/GPQA_diamond.jsonl",
        "content": content,
        "sha256": hashlib.sha256(content.encode()).hexdigest(),
    }
    return run_id, {"run_id": str(run_id), "files": [entry]}


def read(bundle, run_id, **overrides):
    args = {
        "run_id": run_id,
        "model_abbr": "model",
        "dataset": "GPQA_diamond",
        "sample_id": "0",
        "attempt": 1,
    }
    return native_content(bundle, **{**args, **overrides})


def test_real_native_fields_and_multiline_json_are_preserved():
    run_id, bundle = fixture()
    result = read(bundle, run_id)
    assert result["prompt"] == "Actual AISBench prompt"
    assert result["response"] == "Actual response\nwith a line break"
    assert result["native_evidence"]["record_uuid"] == "native-uuid"
    assert (
        len(
            prediction_records(bundle["files"][0]["content"] + "\n" + bundle["files"][0]["content"])
        )
        == 2
    )


def test_identity_retry_checksum_and_duplicate_rejection():
    run_id, bundle = fixture()
    assert read(bundle, run_id, sample_id="1") is None
    assert read(bundle, run_id, attempt=2) is None
    assert read(bundle, run_id, model_abbr="other") is None
    with pytest.raises(ValueError, match="another run"):
        read(bundle, uuid.uuid4())
    bundle["files"].append(dict(bundle["files"][0]))
    with pytest.raises(ValueError, match="Ambiguous"):
        read(bundle, run_id)
    bundle["files"].pop()
    bundle["files"][0]["content"] += "tampered"
    with pytest.raises(ValueError, match="checksum"):
        read(bundle, run_id)
