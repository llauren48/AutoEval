"""Prompt assembly and output validation for the trial-analysis agent demo.

The agent traces why a failed evaluation run failed (which step, what error signal, the
failure category, and remediation) rather than analysing the dataset task content.
Stdlib-only so the demo runs without the AutoEval database or service stack. The schema
mirrors the project's existing analysis pattern: a closed ``evidence_catalog`` of sha256
identities, a prompt that forbids invented citations, and a validator that rejects
malformed or foreign output before anything is persisted.
"""

from __future__ import annotations

import hashlib
import json

SCHEMA_VERSION = "trial-analysis.v1"
RESULT_SCHEMA_VERSION = "trial-analysis-result.v1"
INPUT_SCHEMA_VERSION = "trial-analysis-input.v1"

OUTCOMES = {"success", "partial", "failed", "unknown"}
CATEGORIES = {
    "tool_error",
    "environment",
    "model_behavior",
    "timeout",
    "verifier_mismatch",
    "unexpected_exception",
    "none",
}
_OUTPUT_KEYS = {
    "outcome",
    "summary",
    "verdict",
    "facts",
    "inferences",
    "recommended_actions",
}
_VERDICT_KEYS = {"failed", "category", "root_cause", "evidence_ids"}
_FACT_KEYS = {"statement", "evidence_ids"}


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def sha256_hex(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def trial_dataset(result_body: dict) -> str:
    return str(result_body.get("source") or (result_body.get("task_name") or "").split("/")[0])


def trial_model(result_body: dict) -> str | None:
    model = ((result_body.get("agent_info") or {}).get("model_info") or {}).get("name")
    if model:
        return str(model)
    return ((result_body.get("config") or {}).get("agent") or {}).get("model_name")


def trial_domain(result_body: dict) -> str:
    """Derive the task domain from the AutomationBench task/trial name.

    ``automationbench-support-1585-9c8__uUbJ5pD`` -> ``support``.
    """
    name = str(result_body.get("trial_name") or result_body.get("task_name") or "")
    if "/" in name:
        name = name.rsplit("/", 1)[-1]
    name = name.removeprefix("automationbench-")
    parts = name.split("-")
    domain_parts: list[str] = []
    for part in parts:
        if part.isdigit():
            break
        domain_parts.append(part)
    return "-".join(domain_parts) or "unknown"


def _trim_result(result_body: dict) -> dict:
    """Drop host paths and bulky config noise; keep the trial identity and verdict."""
    keep = {
        key: value
        for key, value in result_body.items()
        if key not in {"task_id", "trial_uri", "config"}
    }
    agent = (result_body.get("config") or {}).get("agent") or {}
    keep["config"] = {
        "agent_name": agent.get("name"),
        "agent_model_name": agent.get("model_name"),
    }
    return keep


def build_analysis_input(
    result_body: dict,
    trajectory_body: dict,
    *,
    verifier_body: dict | None = None,
    max_steps: int = 2000,
    max_input_bytes: int = 512 * 1024,
) -> dict:
    """Freeze the trial into a closed evidence catalog (trial.result + verifier + steps)."""
    steps = trajectory_body.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ValueError("Trajectory has no steps")
    steps = steps[:max_steps]
    step_ids = [step.get("step_id") for step in steps if isinstance(step.get("step_id"), int)]
    if len(step_ids) != len(steps):
        raise ValueError("Trajectory contains a step without an integer step_id")

    catalog = {
        "trial.result": {
            "kind": "trial_result",
            "sha256": sha256_hex(canonical(_trim_result(result_body))),
        }
    }
    evidence = [{"evidence_id": "trial.result", "content": _trim_result(result_body)}]
    if verifier_body is not None:
        catalog["verifier.evaluation"] = {
            "kind": "verifier_evaluation",
            "sha256": sha256_hex(canonical(verifier_body)),
        }
        evidence.append({"evidence_id": "verifier.evaluation", "content": verifier_body})
    for step in steps:
        alias = f"step:{step['step_id']}"
        catalog[alias] = {
            "kind": "trajectory_step",
            "sha256": sha256_hex(canonical(step)),
            "step_id": step["step_id"],
        }
        evidence.append({"evidence_id": alias, "content": step})

    payload = {
        "schema_version": INPUT_SCHEMA_VERSION,
        "evidence_catalog": catalog,
        "evidence": evidence,
    }
    raw = canonical(payload)
    if len(raw) > max_input_bytes:
        payload, raw = _truncate(payload, max_input_bytes)
    return {"payload": payload, "raw": raw, "input_sha256": sha256_hex(raw)}


def _truncate(payload: dict, limit: int) -> tuple[dict, bytes]:
    catalog = payload["evidence_catalog"]
    evidence = [payload["evidence"][0]]
    for entry in payload["evidence"][1:]:
        candidate = canonical(
            {
                "schema_version": payload["schema_version"],
                "evidence_catalog": catalog,
                "evidence": evidence + [entry],
            }
        )
        if len(candidate) > limit:
            break
        evidence.append(entry)
    payload = {
        "schema_version": payload["schema_version"],
        "evidence_catalog": catalog,
        "evidence": evidence,
        "truncated": True,
    }
    return payload, canonical(payload)


def build_prompt(input_payload: dict, *, domain: str, model: str | None) -> str:
    context = f"本案例所属领域为「{domain}」。"
    if model:
        context += f"被测模型为 {model}。"
    return (
        context + "你的任务是诊断一次自动化模型测评运行失败的原因。只分析运行失败本身：哪些轨迹步骤"
        "（step）失败、出现了什么错误信号（exception_info、失败的工具调用、HTTP/API 错误、"
        "超时、或验证器 accuracy 低于 1.0）、可能的失败类别，以及如何修复。如果 evidence 里"
        "存在 verifier.evaluation，优先以其逐条 assertion（passed/failed、期望值与实际行为）"
        "为准来定位具体失败，再回到轨迹步骤找原因。请结合该案例所属领域的特点分析失败（例如"
        "该领域常见的工具、接口或交互模式是否导致了失败）。不要概述或解读数据集的任务内容"
        "（例如工单、邮件、表格或 Slack 内容），那些内容只用于定位失败。"
        '返回一个 JSON 对象，且恰好包含这些键："outcome"、"summary"、"verdict"、"facts"、'
        '"inferences"、"recommended_actions"（没有 "steps" 字段）。'
        "outcome 必须是以下之一："
        + "、".join(sorted(OUTCOMES))
        + '。verdict 是 {"failed", "category", "root_cause", "evidence_ids"}；'
        "category 必须是以下之一："
        + "、".join(sorted(CATEGORIES))
        + '。facts 和 inferences 各自是 {"statement", "evidence_ids"} 对象的列表；'
        "facts 是与失败相关的客观观测（确切的错误、失败的工具调用、exception_info、验证器 "
        "accuracy），inferences 是对失败为何发生的因果推理。evidence_ids 仅允许在 inferences "
        "中为空。recommended_actions 是字符串列表。如果有任何轨迹步骤失败，请在 summary 中"
        "说明具体的步骤 id（例如 step 14）以及每个步骤失败在哪、原因是什么；否则说明没有步骤"
        "失败。verdict.root_cause 在追溯失败原因时，必须明确指出出错的具体轨迹步骤 id（例如 "
        '写 "step 26、step 29"）以及每个步骤发生了什么错误、为什么导致失败，不要只写抽象结论。'
        '每个 evidence_ids 中的条目都必须从 evidence_catalog 复制（别名如 "step:1" '
        '"trial.result" 与 "verifier.evaluation"），且不得重复。不得编造 step id、测量值或分数。outcome 与 verdict '
        "必须一致：outcome 为 success 时要求 failed 为 false 且 category 为 none；outcome 为 "
        "failed 时要求 failed 为 true、root_cause 非空且 category 不为 none。所有描述性字段"
        "（summary、root_cause、statement、recommended_actions）必须使用简体中文；枚举值"
        "（outcome、category）和标识符保持原样。\n" + canonical(input_payload).decode()
    )


def _citations(value, catalog: dict, *, allow_empty: bool = False) -> list:
    if (
        not isinstance(value, list)
        or len(value) > 1000
        or (not allow_empty and not value)
        or any(not isinstance(item, str) for item in value)
        or len(set(value)) != len(value)
        or any(item not in catalog for item in value)
    ):
        raise ValueError("Analysis contains an unknown or duplicate evidence citation")
    return value


def validate_trial_analysis_output(raw, evidence_catalog: dict) -> dict:
    """Reject malformed output, foreign citations, and self-contradictory verdicts."""
    body = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(body, dict) or set(body) != _OUTPUT_KEYS:
        raise ValueError("Trial analysis output schema is invalid")

    if body["outcome"] not in OUTCOMES:
        raise ValueError("Trial analysis outcome is invalid")
    summary = body["summary"]
    if not isinstance(summary, str) or not summary.strip() or len(summary) > 20000:
        raise ValueError("Trial analysis summary is invalid")

    verdict = body["verdict"]
    if not isinstance(verdict, dict) or set(verdict) != _VERDICT_KEYS:
        raise ValueError("Trial analysis verdict is invalid")
    if verdict["category"] not in CATEGORIES:
        raise ValueError("Trial analysis verdict.category is invalid")
    failed_flag = verdict["failed"]
    root_cause = verdict["root_cause"]
    if (
        not isinstance(failed_flag, bool)
        or not isinstance(root_cause, str)
        or len(root_cause) > 4000
    ):
        raise ValueError("Trial analysis verdict fields are invalid")
    _citations(verdict["evidence_ids"], evidence_catalog)
    if failed_flag and (not root_cause.strip() or verdict["category"] == "none"):
        raise ValueError("Failed analysis must state a root cause and category")
    if not failed_flag and verdict["category"] != "none":
        raise ValueError("Non-failed analysis must use category 'none'")
    if body["outcome"] == "failed" and not failed_flag:
        raise ValueError("failed outcome requires verdict.failed=true")
    if body["outcome"] == "success" and failed_flag:
        raise ValueError("success outcome requires verdict.failed=false")

    sections = {}
    for name in ("facts", "inferences"):
        source = body[name]
        if not isinstance(source, list) or len(source) > 100:
            raise ValueError(f"Trial analysis {name} are invalid")
        out = []
        for item in source:
            if not isinstance(item, dict) or set(item) != _FACT_KEYS:
                raise ValueError(f"Trial analysis {name} item is invalid")
            statement = item["statement"]
            if not isinstance(statement, str) or not statement.strip() or len(statement) > 4000:
                raise ValueError(f"Trial analysis {name} statement is invalid")
            _citations(item["evidence_ids"], evidence_catalog, allow_empty=(name == "inferences"))
            out.append(item)
        sections[name] = out

    actions = body["recommended_actions"]
    if (
        not isinstance(actions, list)
        or len(actions) > 100
        or any(
            not isinstance(item, str) or not item.strip() or len(item) > 2000 for item in actions
        )
    ):
        raise ValueError("Trial analysis recommended actions are invalid")

    return {
        "outcome": body["outcome"],
        "summary": summary,
        "verdict": verdict,
        **sections,
        "recommended_actions": actions,
    }
