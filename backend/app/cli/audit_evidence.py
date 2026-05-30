"""审计 reports.report_json 里 evidence_refs / highlights 的「论据 ↔ 论点」对应关系。

核心校验：LLM 给出的 (quote, start_line, end_line) 能否在对应 scene 文本里
被复现 —— 论据真的出现在它声称的行范围内，论点（event_type / hl_type）
才有可信的锚点。

匹配策略（fuzzy，弱版严格度逐级降级）：
1. 在 scene 的 [start_line, end_line] 文本片段里直接 substring 命中 quote
2. 把 quote 切短（取前 12 个非空白字符），再在 line_range 片段里 substring 命中
3. 把 quote 在整场 scene.text 里搜索 —— 命中就是「行号偏移」(line_drift)
4. 全部失败 —— 「无法复现」(unverifiable)

输出：
- 总条数 / 命中分布（exact / short / line_drift / unverifiable）
- 错位条目清单（quote 头 + scene_no + 命中类型）

用法（容器内）：
    docker exec scriptlens_api_dev python -m cli.audit_evidence \
        --script-id d55cebca-fca9-453c-b9f1-d87844fb18bc

    docker exec scriptlens_api_dev python -m cli.audit_evidence --all

业内对照：Hypothes.is 用 prefix/suffix + textQuoteSelector 三段冗余做 fuzzy
anchor，重对位失败时降级到 textPositionSelector。这里是反向 —— 我们已经有
position（line_range），用 quote 反向校验 position 是否被 LLM 自己写错。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Optional

from sqlalchemy import text

from utils.database import engine


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass
class AuditRecord:
    """单条 evidence 的校验结果。"""

    source: str  # "evidence_ref" | "highlight"
    evi_id: str
    scene_id: str
    scene_no: Optional[str]
    event_or_type: Optional[str]
    start_line: Optional[int]
    end_line: Optional[int]
    quote_head: str
    verdict: str  # "exact" | "short" | "line_drift" | "unverifiable" | "no_quote" | "no_range" | "scene_missing"
    detail: str = ""


@dataclass
class AuditReport:
    script_id: str
    script_title: str
    records: list[AuditRecord] = field(default_factory=list)

    def summary(self) -> dict[str, int]:
        c = Counter(r.verdict for r in self.records)
        return dict(c)


# --------------------------------------------------------------------------- #
# 文本归一化 + 匹配
# --------------------------------------------------------------------------- #
_NORM_DROP = re.compile(r"[\s\u3000\u00a0、，。！？：；,.!?:;\"'\\\"'（）()【】\[\]《》<>·…—\-]+")


def _norm(text_: str) -> str:
    """去空白 + 常见中英标点，便于 substring 模糊匹配。"""
    if not text_:
        return ""
    return _NORM_DROP.sub("", text_)


def _slice_lines(scene_text: str, start: Optional[int], end: Optional[int]) -> str:
    """按 1-based 闭区间取行文本；越界自动 clamp。"""
    if not scene_text:
        return ""
    lines = scene_text.replace("\r\n", "\n").split("\n")
    if not lines:
        return ""
    n = len(lines)
    s = max(1, min(start or 1, n))
    e = max(s, min(end or s, n))
    return "\n".join(lines[s - 1 : e])


def _check_quote_against_range(
    *,
    quote: str,
    scene_text: str,
    start: Optional[int],
    end: Optional[int],
) -> tuple[str, str]:
    """返回 (verdict, detail)。"""
    if not quote:
        return ("no_quote", "")
    if start is None or end is None:
        # 没行号 —— 只能验"是否出现在整场"
        nq = _norm(quote)
        if not nq:
            return ("no_quote", "")
        if nq in _norm(scene_text):
            return ("line_drift", "no line_range; full-scene hit")
        return ("unverifiable", "no line_range; quote not in scene")

    range_text = _slice_lines(scene_text, start, end)
    nrange = _norm(range_text)
    nquote = _norm(quote)
    if not nquote:
        return ("no_quote", "")

    if nquote in nrange:
        return ("exact", f"quote ⊂ line[{start},{end}]")

    # 短 quote fallback：取前 12 个非空白字符的核心片段
    short = nquote[:12]
    if len(short) >= 6 and short in nrange:
        return ("short", f"quote_head[{short}] ⊂ line[{start},{end}]")

    nfull = _norm(scene_text)
    if nquote in nfull:
        return ("line_drift", f"quote ⊂ scene but NOT in line[{start},{end}]")

    if len(short) >= 6 and short in nfull:
        return ("line_drift", f"quote_head[{short}] ⊂ scene but NOT in line[{start},{end}]")

    return ("unverifiable", "quote nowhere in scene")


# --------------------------------------------------------------------------- #
# DB 拉数据
# --------------------------------------------------------------------------- #
def _load_script(script_id: str) -> tuple[str, dict[str, Any], dict[str, str]]:
    """返回 (title, report_json, scene_id -> scene_text)。"""
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT s.title, r.report_json
                FROM scriptlens.reports r
                JOIN scriptlens.scripts s ON s.id = r.script_id
                WHERE r.script_id = :sid
                """
            ),
            {"sid": script_id},
        ).mappings().first()
        if not row:
            raise SystemExit(f"no report found for script_id={script_id}")
        scenes = conn.execute(
            text(
                "SELECT id::text AS id, scene_no, text FROM scriptlens.scenes WHERE script_id = :sid"
            ),
            {"sid": script_id},
        ).mappings().all()
    text_by_sid = {sc["id"]: sc["text"] or "" for sc in scenes}
    no_by_sid = {sc["id"]: sc["scene_no"] for sc in scenes}
    return row["title"], row["report_json"], (text_by_sid, no_by_sid)


def _list_all_scripts_with_reports() -> list[tuple[str, str]]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT s.id::text AS id, s.title
                FROM scriptlens.reports r
                JOIN scriptlens.scripts s ON s.id = r.script_id
                ORDER BY r.generated_at DESC
                """
            )
        ).mappings().all()
    return [(r["id"], r["title"]) for r in rows]


# --------------------------------------------------------------------------- #
# 单剧本审计
# --------------------------------------------------------------------------- #
def _audit_one(script_id: str) -> AuditReport:
    title, report_json, (text_by_sid, no_by_sid) = _load_script(script_id)
    rep = AuditReport(script_id=script_id, script_title=title)

    evidence_refs = report_json.get("evidence_refs") or []
    highlights = report_json.get("highlights") or []

    for er in evidence_refs:
        sid = str(er.get("scene_id") or "")
        scene_text = text_by_sid.get(sid)
        verified = bool(er.get("quote_verified", False))
        quote = str(er.get("quote") or "")
        if scene_text is None:
            rep.records.append(
                AuditRecord(
                    source="evidence_ref",
                    evi_id=str(er.get("id") or ""),
                    scene_id=sid,
                    scene_no=no_by_sid.get(sid),
                    event_or_type=str(er.get("quote_source") or ""),
                    start_line=er.get("start_line"),
                    end_line=er.get("end_line"),
                    quote_head=quote[:40],
                    verdict="scene_missing",
                )
            )
            continue

        # 按 verified 状态走两套校验：
        # - verified=true: quote 必须实打实是原文 → 跑 exact/short/line_drift/unverifiable 判定
        # - verified=false: quote 应为空 (v3.5 契约)；如果有内容那是 legacy 残留
        if verified:
            verdict, detail = _check_quote_against_range(
                quote=quote,
                scene_text=scene_text,
                start=er.get("start_line"),
                end=er.get("end_line"),
            )
        else:
            if quote:
                verdict = "legacy_filled"
                detail = "quote 非空但 verified=false（应为空，可能是旧契约残留）"
            else:
                verdict = "by_design_empty"
                detail = "verified=false 且 quote 为空（符合 v3.5 契约）"

        rep.records.append(
            AuditRecord(
                source="evidence_ref",
                evi_id=str(er.get("id") or ""),
                scene_id=sid,
                scene_no=no_by_sid.get(sid),
                event_or_type=str(er.get("quote_source") or ""),
                start_line=er.get("start_line"),
                end_line=er.get("end_line"),
                quote_head=quote[:40],
                verdict=verdict,
                detail=detail,
            )
        )

    for hl in highlights:
        sid = str(hl.get("scene_id") or "")
        scene_text = text_by_sid.get(sid)
        verified = bool(hl.get("quote_verified", False))
        # highlights 新契约：quote 是 verbatim（verified 时填）；evidence 是 legacy 兼容
        quote = str(hl.get("quote") or "")
        if not quote and not verified:
            # 旧报告兼容：legacy evidence 字段可能仍存 claim
            legacy_evidence = str(hl.get("evidence") or "")
            quote = legacy_evidence

        if scene_text is None:
            rep.records.append(
                AuditRecord(
                    source="highlight",
                    evi_id=str(hl.get("id") or ""),
                    scene_id=sid,
                    scene_no=no_by_sid.get(sid),
                    event_or_type=str(hl.get("type") or ""),
                    start_line=hl.get("start_line"),
                    end_line=hl.get("end_line"),
                    quote_head=quote[:40],
                    verdict="scene_missing",
                )
            )
            continue

        if verified:
            verdict, detail = _check_quote_against_range(
                quote=str(hl.get("quote") or ""),
                scene_text=scene_text,
                start=hl.get("start_line"),
                end=hl.get("end_line"),
            )
        else:
            if str(hl.get("quote") or ""):
                verdict = "legacy_filled"
                detail = "highlight.quote 非空但 verified=false"
            else:
                verdict = "by_design_empty"
                detail = "verified=false 且 highlight.quote 为空（符合 v3.5 契约）"

        rep.records.append(
            AuditRecord(
                source="highlight",
                evi_id=str(hl.get("id") or ""),
                scene_id=sid,
                scene_no=no_by_sid.get(sid),
                event_or_type=str(hl.get("type") or ""),
                start_line=hl.get("start_line"),
                end_line=hl.get("end_line"),
                quote_head=quote[:40],
                verdict=verdict,
                detail=detail,
            )
        )
    return rep


# --------------------------------------------------------------------------- #
# 输出
# --------------------------------------------------------------------------- #
def _print_report(rep: AuditReport, *, show_detail: bool, json_out: bool) -> None:
    if json_out:
        payload = {
            "script_id": rep.script_id,
            "script_title": rep.script_title,
            "summary": rep.summary(),
            "records": [r.__dict__ for r in rep.records],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    n = len(rep.records)
    smry = rep.summary()
    exact = smry.get("exact", 0)
    short = smry.get("short", 0)
    line_drift = smry.get("line_drift", 0)
    unverifiable = smry.get("unverifiable", 0)
    by_design_empty = smry.get("by_design_empty", 0)
    legacy_filled = smry.get("legacy_filled", 0)
    other = sum(
        v for k, v in smry.items()
        if k not in {"exact", "short", "line_drift", "unverifiable", "by_design_empty", "legacy_filled"}
    )

    verified_n = exact + short + line_drift + unverifiable
    print(f"=== {rep.script_title} ({rep.script_id}) ===")
    print(f"  evidence_refs + highlights total = {n}")
    print(f"  --- quote_verified=true 路径（必须 verbatim）{verified_n} 条 ---")
    print(f"    exact (quote ⊂ line_range)        = {exact} ({_pct(exact, verified_n)})")
    print(f"    short (quote_head ⊂ line_range)   = {short} ({_pct(short, verified_n)})")
    print(f"    line_drift (in scene, off range)  = {line_drift} ({_pct(line_drift, verified_n)})")
    print(f"    unverifiable (not in scene)       = {unverifiable} ({_pct(unverifiable, verified_n)})")
    print(f"  --- quote_verified=false 路径（quote 应为空，走整场跳转）---")
    print(f"    by_design_empty (契约正确)         = {by_design_empty}")
    print(f"    legacy_filled  (旧契约残留 ⚠ )     = {legacy_filled}")
    if other:
        print(f"  other (no_quote/no_range/scene_missing) = {other}")

    if show_detail:
        bad = [r for r in rep.records if r.verdict in {"line_drift", "unverifiable", "legacy_filled"}]
        if bad:
            print("\n  --- problematic records ---")
            for r in bad:
                print(
                    f"  [{r.verdict:13s}] {r.source:13s} "
                    f"scene={r.scene_no or r.scene_id[:8]} "
                    f"line=[{r.start_line},{r.end_line}] "
                    f"type={r.event_or_type} "
                    f"quote_head=「{r.quote_head}」 "
                    f"{r.detail}"
                )
    print()


def _pct(num: int, total: int) -> str:
    if total <= 0:
        return "—"
    return f"{num * 100 / total:.1f}%"


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Audit evidence quote vs line_range correspondence")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--script-id", help="single script uuid")
    g.add_argument("--all", action="store_true", help="audit every script with a report")
    p.add_argument("--show-detail", action="store_true", help="print problematic records")
    p.add_argument("--json", action="store_true", help="emit JSON instead of human text")
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    if args.script_id:
        targets = [(args.script_id, "")]
    else:
        targets = _list_all_scripts_with_reports()

    overall_summary: Counter[str] = Counter()
    overall_total = 0
    for sid, _title in targets:
        try:
            rep = _audit_one(sid)
        except SystemExit as ex:
            print(f"skip {sid}: {ex}", file=sys.stderr)
            continue
        _print_report(rep, show_detail=args.show_detail, json_out=args.json)
        overall_total += len(rep.records)
        for k, v in rep.summary().items():
            overall_summary[k] += v

    if not args.json and len(targets) > 1:
        print(f"=== ALL ({len(targets)} reports, {overall_total} records) ===")
        for k in ("exact", "short", "line_drift", "unverifiable"):
            n = overall_summary.get(k, 0)
            print(f"  {k:13s} {n} ({_pct(n, overall_total)})")


if __name__ == "__main__":
    main()
