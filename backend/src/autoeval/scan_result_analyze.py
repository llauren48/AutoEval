"""Runnable demo: scan a directory of AutomationBench trials and analyse them.

Each trial is a ``result.json`` with a ``trajectory.json`` beside it (or in an ``agent/``
subdirectory) and, optionally, a ``verifier/evaluation.json`` holding per-assertion
verdicts. Trials can be filtered by dataset (substring of ``source``/``task_name``) or by
model name.

Examples (run from the repository root)::

    python backend/src/autoeval/scan_result_analyze.py --dir result
    python backend/src/autoeval/scan_result_analyze.py --dir result --dataset automationbench
    python backend/src/autoeval/scan_result_analyze.py --dir result --model deepseek-flash
    python backend/src/autoeval/scan_result_analyze.py --dir result --dsh /path/to/dsh

When ``dsh`` is not found (or ``--mock`` is passed) a deterministic mock heuristic is used
instead; it makes no paid call and is labelled as ``mock-heuristic`` in the output. The
structured result is written to ``<dir>/analysis_out/<trial_id>.json`` — this is the exact
payload that the planned ``trial_analysis`` table will later reference via S3.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

try:
    from autoeval.dsh_agent import dsh_available, dsh_version, mock_analysis, run_dsh
    from autoeval.trial_analysis_schema import (
        RESULT_SCHEMA_VERSION,
        build_analysis_input,
        build_prompt,
        trial_dataset,
        trial_domain,
        trial_model,
        validate_trial_analysis_output,
    )
except ImportError:  # run as a plain script from the repository root
    from dsh_agent import dsh_available, dsh_version, mock_analysis, run_dsh
    from trial_analysis_schema import (
        RESULT_SCHEMA_VERSION,
        build_analysis_input,
        build_prompt,
        trial_dataset,
        trial_domain,
        trial_model,
        validate_trial_analysis_output,
    )


def _load_json(path: Path) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def discover_trials(root: Path) -> list[tuple[Path, Path, Path, Path | None]]:
    """Return (directory, result_path, trajectory_path, verifier_path) per trial.

    Two trajectory layouts are supported: a ``trajectory.json`` sitting next to
    ``result.json``, and the newer ``agent/trajectory.json`` layout where the trajectory
    lives in an ``agent/`` subdirectory of the trial directory. ``verifier_path`` is the
    optional ``verifier/evaluation.json`` holding the per-assertion verdicts.
    """
    trials = []
    for result_path in sorted(root.rglob("result.json")):
        if "analysis_out" in result_path.parts:
            continue
        trajectory_path = result_path.with_name("trajectory.json")
        if not trajectory_path.exists():
            trajectory_path = result_path.parent / "agent" / "trajectory.json"
        if not trajectory_path.exists():
            continue
        verifier_path = result_path.parent / "verifier" / "evaluation.json"
        trials.append(
            (
                result_path.parent,
                result_path,
                trajectory_path,
                verifier_path if verifier_path.exists() else None,
            )
        )
    return trials


def analyze_one(
    result_body: dict,
    trajectory_body: dict,
    *,
    verifier_body: dict | None,
    use_mock: bool,
    dsh: str | None,
    timeout: int,
) -> dict:
    trial_id = result_body.get("id")
    domain = trial_domain(result_body)
    model = trial_model(result_body)
    built = build_analysis_input(result_body, trajectory_body, verifier_body=verifier_body)
    evidence_catalog = built["payload"]["evidence_catalog"]

    use_dsh = (not use_mock) and (dsh is not None or dsh_available())
    if use_dsh:
        native = run_dsh(
            build_prompt(built["payload"], domain=domain, model=model),
            timeout=timeout,
            binary=dsh,
        )
        analyzer = native.analyzer
        error_code = native.error_code
        raw_response = native.raw
        parsed = None
        state = native.state
        if native.state == "succeeded":
            try:
                parsed = validate_trial_analysis_output(native.response, evidence_catalog)
                state, error_code = "succeeded", None
            except ValueError:
                state, error_code = "failed", "INVALID_MODEL_OUTPUT"
    else:
        parsed = validate_trial_analysis_output(
            mock_analysis(result_body, trajectory_body), evidence_catalog
        )
        state, error_code, raw_response, analyzer = "succeeded", None, None, "mock-heuristic"

    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "trial_id": trial_id,
        "dataset": trial_dataset(result_body),
        "model": model,
        "domain": domain,
        "input_sha256": built["input_sha256"],
        "state": state,
        "analysis": parsed,
        "raw_response": raw_response,
        "error_code": error_code,
        "analyzer": analyzer,
        "dsh_binary": dsh if use_dsh else None,
        "dsh_version": dsh_version(dsh) if use_dsh else None,
    }


def summarize_domains(results: list[dict]) -> list[dict]:
    """Aggregate analysed trials by domain and flag error-concentrated domains."""
    buckets: dict[str, dict] = {}
    for result in results:
        domain = result.get("domain") or "unknown"
        bucket = buckets.setdefault(
            domain, {"domain": domain, "total": 0, "failed": 0, "categories": {}}
        )
        bucket["total"] += 1
        analysis = result.get("analysis")
        if analysis is None:
            failed, category = True, "unanalysed"
        else:
            failed = bool(analysis["verdict"]["failed"])
            category = analysis["verdict"]["category"]
        if failed:
            bucket["failed"] += 1
            bucket["categories"][category] = bucket["categories"].get(category, 0) + 1

    summary = []
    for bucket in buckets.values():
        total, failed = bucket["total"], bucket["failed"]
        summary.append(
            {
                "domain": bucket["domain"],
                "total": total,
                "failed": failed,
                "failure_rate": round(failed / total, 3) if total else 0.0,
                "categories": bucket["categories"],
                "error_prone": total > 0 and failed / total >= 0.5,
            }
        )
    summary.sort(key=lambda item: (-item["failed"], -item["failure_rate"], item["domain"]))
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", default="result", help="directory to scan (default: ./result)")
    parser.add_argument("--dataset", help="filter by dataset (substring of source/task_name)")
    parser.add_argument("--model", help="filter by model name (case-insensitive)")
    parser.add_argument("--dsh", help="path to the dsh binary (enables real dsh instead of mock)")
    parser.add_argument("--timeout", type=int, default=300, help="dsh timeout in seconds")
    parser.add_argument("--out-dir", help="output directory (default: <dir>/analysis_out)")
    parser.add_argument("--mock", action="store_true", help="force mock even when dsh is available")
    args = parser.parse_args(argv)

    root = Path(args.dir)
    trials = discover_trials(root)
    if not trials:
        print(f"No trial (result.json + trajectory.json) found under {root}")
        return 1

    out_dir = Path(args.out_dir) if args.out_dir else root / "analysis_out"
    out_dir.mkdir(parents=True, exist_ok=True)

    matched = 0
    results = []
    for directory, result_path, trajectory_path, verifier_path in trials:
        result_body = _load_json(result_path)
        trajectory_body = _load_json(trajectory_path)
        verifier_body = _load_json(verifier_path) if verifier_path else None
        dataset = trial_dataset(result_body)
        model = trial_model(result_body)
        domain = trial_domain(result_body)
        task_name = str(result_body.get("task_name") or "")
        if (
            args.dataset
            and args.dataset.lower() not in dataset.lower()
            and args.dataset.lower() not in task_name.lower()
        ):
            continue
        if args.model and (model or "").lower() != args.model.lower():
            continue

        matched += 1
        print(f"== {result_path}  (dataset={dataset}, model={model}, domain={domain})")
        result = analyze_one(
            result_body,
            trajectory_body,
            verifier_body=verifier_body,
            use_mock=args.mock,
            dsh=args.dsh,
            timeout=args.timeout,
        )
        results.append(result)
        out_path = out_dir / f"{result['trial_id']}.json"
        out_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

        analysis = result["analysis"]
        outcome = analysis["outcome"] if analysis else "n/a"
        print(f"   outcome={outcome}  state={result['state']}  analyzer={result['analyzer']}")
        if analysis:
            verdict = analysis["verdict"]
            print(f"   verdict.failed={verdict['failed']}  category={verdict['category']}")
            if verdict["failed"]:
                print(f"   root_cause={verdict['root_cause']}")
            print(f"   summary={analysis['summary']}")
        elif result["error_code"]:
            print(f"   error_code={result['error_code']}")
        print(f"   -> {out_path}")

    print(f"Scanned {len(trials)} trial(s), analysed {matched}.")

    domain_summary = summarize_domains(results)
    if domain_summary:
        summary_path = out_dir / "_domain_summary.json"
        summary_path.write_text(
            json.dumps(domain_summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print("\n== 领域汇总 ==")
        for item in domain_summary:
            flag = "  [错误集中]" if item["error_prone"] else ""
            print(
                f"   {item['domain']}: total={item['total']} failed={item['failed']} "
                f"failure_rate={item['failure_rate']}{flag}"
            )
        print(f"   -> {summary_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
