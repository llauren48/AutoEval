"""Thin subprocess boundary around the ``dsh`` CLI for trial analysis.

The real ``dsh`` invocation is not finalised yet, so this module keeps the process
boundary in one place. :func:`run_dsh` invokes the CLI and parses one JSON object from
stdout; :func:`mock_analysis` is a deterministic, clearly-labelled heuristic stand-in
that never makes a paid call. Both feed the same validation path in
``trial_analysis_schema``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass

DEFAULT_BINARY = "dsh"

# Coarse marker used only by the mock heuristic; the real failure localisation is the
# agent's job via ``dsh``.
_FAILURE_RE = re.compile(r"error|exception|failed|denied|not found|timeout|refused", re.IGNORECASE)


@dataclass
class DshResult:
    state: str
    response: dict | None
    raw: str | None
    error_code: str | None
    analyzer: str


def dsh_binary() -> str:
    return os.environ.get("AE_DSH_BINARY", DEFAULT_BINARY)


def dsh_available(binary: str | None = None) -> bool:
    return shutil.which(binary or dsh_binary()) is not None


def dsh_version(binary: str | None = None) -> str | None:
    """Best-effort version probe; ``dsh`` may not support ``--version``."""
    path = shutil.which(binary or dsh_binary())
    if path is None:
        return None
    try:
        proc = subprocess.run(
            [path, "--version"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = (proc.stdout or "").strip() or (proc.stderr or "").strip()
    return text.splitlines()[0] if text else None


def _extract_json(text: str) -> dict:
    """Best-effort parse of the single JSON object the prompt asked the agent for.

    ``dsh`` headless prints an agent transcript, not a bare JSON document, so the model's
    JSON is usually wrapped in markdown fences or surrounded by prose. Try, in order: a
    fenced ````` ```json ```` ``` block, then the outermost balanced ``{...}`` span, then
    the whole text.
    """
    candidates = []
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        candidates.append(fence.group(1).strip())
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    candidates.append(text)
    for candidate in candidates:
        if not candidate:
            continue
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    raise ValueError("no JSON object found in dsh output")


def run_dsh(prompt: str, *, timeout: int = 300, binary: str | None = None) -> DshResult:
    """Invoke ``dsh`` in one-shot headless mode and extract one JSON object from stdout.

    The task is piped over stdin via the ``-`` placeholder (``dsh --profile headless -``),
    which avoids the OS argument-size limit that a positional task would hit on Windows.
    ``AE_DSH_PROFILE`` overrides the profile (default ``headless``); ``AE_DSH_ARGS``
    (space separated) supplies extra flags such as model selection. ``shell=True`` is
    never used.
    """
    binary = binary or dsh_binary()
    path = shutil.which(binary)
    if path is None:
        return DshResult("failed", None, None, "DSH_NOT_FOUND", "dsh")
    profile = os.environ.get("AE_DSH_PROFILE", "headless")
    extra = [arg for arg in os.environ.get("AE_DSH_ARGS", "").split() if arg]
    # `-` tells dsh to read the task from stdin; `input=` carries the (possibly large)
    # prompt without putting it on the command line.
    command = [path, "--profile", profile, *extra, "-"]
    try:
        proc = subprocess.run(
            command,
            check=False,
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return DshResult("unresolved", None, None, "DSH_TIMEOUT", "dsh")
    except OSError:
        return DshResult("failed", None, None, "DSH_EXEC_FAILED", "dsh")
    stdout = (proc.stdout or "").strip()
    if proc.returncode != 0:
        return DshResult(
            "failed", None, stdout or (proc.stderr or "").strip(), "DSH_EMPTY_OR_ERROR", "dsh"
        )
    try:
        payload = _extract_json(stdout)
    except ValueError:
        return DshResult("failed", None, stdout, "DSH_INVALID_JSON", "dsh")
    return DshResult("succeeded", payload, stdout, None, "dsh")


def mock_analysis(result_body: dict, trajectory_body: dict) -> dict:
    """Deterministic heuristic stand-in for ``dsh``; no paid call is made.

    Trial-level verdict comes from the real signal (``verifier_result`` accuracy and
    ``exception_info``). Step-level flagging is intentionally shallow: it only scans the
    agent's own message/reasoning text, not the API discovery catalog, so a success case
    is not spuriously marked failed. The returned dict matches ``trial-analysis.v1``.
    """
    verifier = result_body.get("verifier_result") or {}
    accuracy = (verifier.get("rewards") or {}).get("accuracy")
    exception = result_body.get("exception_info")
    steps = trajectory_body.get("steps") or []

    if exception is not None:
        outcome = "failed"
    elif accuracy is None:
        outcome = "unknown"
    elif accuracy >= 1.0:
        outcome = "success"
    elif accuracy > 0:
        outcome = "partial"
    else:
        outcome = "failed"

    failed = outcome in {"failed", "partial"}
    failed_steps = []
    for step in steps:
        step_id = step.get("step_id")
        if not isinstance(step_id, int):
            continue
        # Only localise failing steps when the trial itself failed/partial; on a
        # successful trial the agent's own text routinely mentions "error/timeout" while
        # recovering, which would be a false positive.
        text = " ".join(str(step.get(key) or "") for key in ("message", "reasoning_content"))
        if failed and _FAILURE_RE.search(text):
            failed_steps.append(step_id)

    if exception is not None:
        category = "unexpected_exception"
        root_cause = f"评测出现未预期异常：{exception}"
    elif not failed:
        category = "none"
        root_cause = "未检测到失败"
    elif failed_steps:
        category = "tool_error"
        root_cause = (
            f"验证器 accuracy={accuracy}；{len(failed_steps)} 个步骤被标记为失败：{failed_steps}"
        )
    else:
        category = "verifier_mismatch"
        root_cause = f"验证器 accuracy={accuracy}，但未定位到明显的失败步骤"

    if failed_steps:
        summary = (
            f"mock 启发式分析：outcome={outcome}，accuracy={accuracy}。"
            f"步骤 {failed_steps} 出现错误：这些步骤的 agent 文本命中了失败特征"
            "（error/exception/timeout/refused 等），可能为工具调用失败或环境异常，"
            "需复核对应步骤的 observation 与工具输出。"
        )
    else:
        summary = f"mock 启发式分析：outcome={outcome}，accuracy={accuracy}，未检测到失败步骤。"

    return {
        "outcome": outcome,
        "summary": summary,
        "verdict": {
            "failed": failed,
            "category": category,
            "root_cause": root_cause,
            "evidence_ids": ["trial.result"],
        },
        "facts": [
            {"statement": f"验证器 accuracy={accuracy}", "evidence_ids": ["trial.result"]},
            {
                "statement": "exception_info " + ("存在" if exception is not None else "不存在"),
                "evidence_ids": ["trial.result"],
            },
        ],
        "inferences": (
            [
                {
                    "statement": f"步骤 {failed_steps} 出现失败特征",
                    "evidence_ids": [f"step:{i}" for i in failed_steps],
                }
            ]
            if failed_steps
            else []
        ),
        "recommended_actions": (
            ["请复核被标记步骤的 observation 与工具输出。"] if failed_steps else []
        ),
    }
