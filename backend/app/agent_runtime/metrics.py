"""
简单的工具执行指标收集
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from threading import Lock
from typing import Any, Dict, Optional


@dataclass
class ToolMetric:
    success: int = 0
    failure: int = 0
    total_duration: float = 0.0


@dataclass
class LLMUsageMetric:
    success: int = 0
    failure: int = 0
    total_duration: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    total_cost: float = 0.0


_tool_metrics: Dict[str, ToolMetric] = defaultdict(ToolMetric)
_intent_metrics: Dict[str, Dict[str, int]] = defaultdict(lambda: {"low": 0, "medium": 0, "high": 0})
_plan_metrics: Dict[str, "PlanMetric"] = defaultdict(lambda: PlanMetric())
_llm_metrics: Dict[str, LLMUsageMetric] = defaultdict(LLMUsageMetric)

_workspace_cache_events: Dict[str, int] = defaultdict(int)
_workspace_scan_metric = {"count": 0, "total": 0.0}
_feedback_metrics: Dict[str, int] = defaultdict(int)
_lock = Lock()


@dataclass
class PlanMetric:
    count: int = 0
    total_tools: int = 0
    total_duration: float = 0.0

    def record(self, tool_count: int, duration: float) -> None:
        self.count += 1
        self.total_tools += tool_count
        self.total_duration += duration


def record_tool_metric(tool_name: str, success: bool, duration: float):
    with _lock:
        metric = _tool_metrics[tool_name]
        if success:
            metric.success += 1
        else:
            metric.failure += 1
        metric.total_duration += duration


def record_intent_metric(intent: str, confidence: float):
    bucket = "low"
    if confidence >= 0.8:
        bucket = "high"
    elif confidence >= 0.5:
        bucket = "medium"

    with _lock:
        _intent_metrics[intent][bucket] += 1


def record_plan_metric(intent: str, tool_count: int, duration: float):
    with _lock:
        _plan_metrics[intent].record(tool_count, duration)


def record_workspace_cache_event(event: str):
    with _lock:
        _workspace_cache_events[event] += 1


def record_workspace_scan(duration: float):
    with _lock:
        _workspace_scan_metric["count"] += 1
        _workspace_scan_metric["total"] += duration


def record_user_feedback(rating: str, trace_id: Optional[str] = None):
    with _lock:
        _feedback_metrics[rating] += 1


def record_llm_usage(
    provider: str,
    model: str,
    success: bool,
    duration: float,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int = 0,
    cost: float = 0.0,
):
    key = f"{provider}::{model}"
    with _lock:
        metric = _llm_metrics[key]
        if success:
            metric.success += 1
        else:
            metric.failure += 1
        metric.total_duration += duration
        metric.prompt_tokens += int(prompt_tokens or 0)
        metric.completion_tokens += int(completion_tokens or 0)
        metric.total_tokens += int(total_tokens or (prompt_tokens or 0) + (completion_tokens or 0))
        metric.total_cost += float(cost or 0.0)


def format_prometheus_metrics() -> str:
    lines = [
        "# HELP doc_studio_tool_calls_total Number of tool executions.",
        "# TYPE doc_studio_tool_calls_total counter",
    ]
    with _lock:
        for tool_name, metric in _tool_metrics.items():
            lines.append(
                f'doc_studio_tool_calls_total{{tool="{tool_name}",status="success"}} {metric.success}'
            )
            lines.append(
                f'doc_studio_tool_calls_total{{tool="{tool_name}",status="failure"}} {metric.failure}'
            )
        lines.append("# HELP doc_studio_tool_duration_seconds_total Sum of tool execution duration.")
        lines.append("# TYPE doc_studio_tool_duration_seconds_total gauge")
        for tool_name, metric in _tool_metrics.items():
            lines.append(
                f'doc_studio_tool_duration_seconds_total{{tool="{tool_name}"}} {metric.total_duration:.6f}'
            )
        lines.append("# HELP doc_studio_intent_classifications_total Number of intent classifications by confidence.")
        lines.append("# TYPE doc_studio_intent_classifications_total counter")
        for intent, buckets in _intent_metrics.items():
            for bucket, value in buckets.items():
                lines.append(
                    f'doc_studio_intent_classifications_total{{intent="{intent}",confidence="{bucket}"}} {value}'
                )
        lines.append("# HELP doc_studio_plan_build_seconds_total Total time spent building plans.")
        lines.append("# TYPE doc_studio_plan_build_seconds_total gauge")
        for intent, metric in _plan_metrics.items():
            lines.append(
                f'doc_studio_plan_build_seconds_total{{intent="{intent}"}} {metric.total_duration:.6f}'
            )
        lines.append("# HELP doc_studio_plan_build_count Number of plans built per intent.")
        lines.append("# TYPE doc_studio_plan_build_count counter")
        for intent, metric in _plan_metrics.items():
            lines.append(
                f'doc_studio_plan_build_count{{intent="{intent}"}} {metric.count}'
            )
        lines.append("# HELP doc_studio_plan_average_tools Average number of tools per plan.")
        lines.append("# TYPE doc_studio_plan_average_tools gauge")
        for intent, metric in _plan_metrics.items():
            average = metric.total_tools / metric.count if metric.count else 0.0
            lines.append(
                f'doc_studio_plan_average_tools{{intent="{intent}"}} {average:.6f}'
            )
        lines.append("# HELP doc_studio_workspace_cache_events_total Workspace cache events by type.")
        lines.append("# TYPE doc_studio_workspace_cache_events_total counter")
        for event, value in _workspace_cache_events.items():
            lines.append(
                f'doc_studio_workspace_cache_events_total{{event="{event}"}} {value}'
            )
        lines.append("# HELP doc_studio_workspace_scan_duration_seconds_total Total time spent scanning workspace files.")
        lines.append("# TYPE doc_studio_workspace_scan_duration_seconds_total gauge")
        lines.append(
            f'doc_studio_workspace_scan_duration_seconds_total {_workspace_scan_metric["total"]:.6f}'
        )
        lines.append("# HELP doc_studio_workspace_scan_operations_total Number of workspace scans.")
        lines.append("# TYPE doc_studio_workspace_scan_operations_total counter")
        lines.append(
            f'doc_studio_workspace_scan_operations_total {_workspace_scan_metric["count"]}'
        )
        lines.append("# HELP doc_studio_user_feedback_total User feedback counts by rating.")
        lines.append("# TYPE doc_studio_user_feedback_total counter")
        for rating, value in _feedback_metrics.items():
            lines.append(
                f'doc_studio_user_feedback_total{{rating="{rating}"}} {value}'
            )
        lines.append("# HELP doc_studio_llm_requests_total LLM request counts.")
        lines.append("# TYPE doc_studio_llm_requests_total counter")
        for key, metric in _llm_metrics.items():
            provider, model = key.split("::", 1)
            lines.append(
                f'doc_studio_llm_requests_total{{provider="{provider}",model="{model}",status="success"}} {metric.success}'
            )
            lines.append(
                f'doc_studio_llm_requests_total{{provider="{provider}",model="{model}",status="failure"}} {metric.failure}'
            )
        lines.append("# HELP doc_studio_llm_duration_seconds_total Total LLM latency (seconds).")
        lines.append("# TYPE doc_studio_llm_duration_seconds_total gauge")
        for key, metric in _llm_metrics.items():
            provider, model = key.split("::", 1)
            lines.append(
                f'doc_studio_llm_duration_seconds_total{{provider="{provider}",model="{model}"}} {metric.total_duration:.6f}'
            )
        lines.append("# HELP doc_studio_llm_tokens_total Total tokens by type.")
        lines.append("# TYPE doc_studio_llm_tokens_total counter")
        for key, metric in _llm_metrics.items():
            provider, model = key.split("::", 1)
            lines.append(
                f'doc_studio_llm_tokens_total{{provider="{provider}",model="{model}",type="prompt"}} {metric.prompt_tokens}'
            )
            lines.append(
                f'doc_studio_llm_tokens_total{{provider="{provider}",model="{model}",type="completion"}} {metric.completion_tokens}'
            )
            lines.append(
                f'doc_studio_llm_tokens_total{{provider="{provider}",model="{model}",type="total"}} {metric.total_tokens}'
            )
        lines.append("# HELP doc_studio_llm_cost_total Estimated LLM cost.")
        lines.append("# TYPE doc_studio_llm_cost_total counter")
        for key, metric in _llm_metrics.items():
            provider, model = key.split("::", 1)
            lines.append(
                f'doc_studio_llm_cost_total{{provider="{provider}",model="{model}"}} {metric.total_cost:.6f}'
            )
    return "\n".join(lines) + "\n"


def collect_metrics_summary() -> Dict[str, Any]:
    """Collect a compact metrics summary for quick inspection."""

    with _lock:
        tool_stats = {}
        for tool_name, metric in _tool_metrics.items():
            total = metric.success + metric.failure
            avg_duration = metric.total_duration / total if total else 0.0
            tool_stats[tool_name] = {
                "success": metric.success,
                "failure": metric.failure,
                "total": total,
                "avg_duration_seconds": round(avg_duration, 4),
            }

        plan_stats = {}
        for intent, metric in _plan_metrics.items():
            avg_tools = metric.total_tools / metric.count if metric.count else 0.0
            avg_duration = metric.total_duration / metric.count if metric.count else 0.0
            plan_stats[intent] = {
                "count": metric.count,
                "avg_tools": round(avg_tools, 2),
                "avg_duration_seconds": round(avg_duration, 4),
            }

        return {
            "tools": tool_stats,
            "intents": {intent: dict(buckets) for intent, buckets in _intent_metrics.items()},
            "plans": plan_stats,
            "llm": {
                key: {
                    "success": metric.success,
                    "failure": metric.failure,
                    "total": metric.success + metric.failure,
                    "avg_duration_seconds": round(
                        metric.total_duration / (metric.success + metric.failure)
                        if (metric.success + metric.failure)
                        else 0.0,
                        4,
                    ),
                    "prompt_tokens": metric.prompt_tokens,
                    "completion_tokens": metric.completion_tokens,
                    "total_tokens": metric.total_tokens,
                    "total_cost": round(metric.total_cost, 6),
                }
                for key, metric in _llm_metrics.items()
            },
            "workspace_scans": {
                "count": _workspace_scan_metric["count"],
                "total_duration_seconds": round(_workspace_scan_metric["total"], 4),
            },
            "workspace_cache_events": dict(_workspace_cache_events),
            "feedback": dict(_feedback_metrics),
        }

