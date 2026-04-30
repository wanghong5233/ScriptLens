from app.core.models import BasicReport, ScriptDocument, ScriptSegment


CHARACTER_CANDIDATES = ("秀秀", "陆寻", "周通", "王秦", "赵瑟瑟", "林昭", "小翠", "管家")


def generate_basic_report(document: ScriptDocument, segments: list[ScriptSegment]) -> BasicReport:
    if not segments:
        raise ValueError("Cannot generate report without segments.")

    full_text = document.raw_text
    characters = [name for name in CHARACTER_CANDIDATES if name in full_text]

    return BasicReport(
        script_id=document.id,
        title=document.title,
        summary=_summary(document, segments),
        core_plot=_core_plot(full_text),
        main_characters=characters or ["待后续 LLM 抽取"],
        key_conflicts=_key_conflicts(full_text),
        hooks=_hooks(full_text),
        risks=_risks(full_text),
        next_step=_next_step(full_text),
        segments=segments,
    )


def _summary(document: ScriptDocument, segments: list[ScriptSegment]) -> str:
    return (
        f"已读取《{document.title}》并识别出 {len(segments)} 个剧情片段。"
        "D3 基础报告使用确定性规则生成,用于验证上传、分段、报告和前端展示链路。"
    )


def _core_plot(text: str) -> str:
    if all(keyword in text for keyword in ("胡人", "将军", "孩子")):
        return "女主在战乱和身份压迫中求生,依附将军获得短暂庇护,又因旧爱回归和孩子身世被卷入新的关系冲突。"
    return "当前版本已完成文本分段,核心主线将在后续 LLM 抽取链路中生成。"


def _key_conflicts(text: str) -> list[str]:
    conflicts: list[str] = []
    if "胡人" in text or "战乱" in text:
        conflicts.append("外部生存危机:战乱、屠村和暴力威胁推动开篇。")
    if "侍妾" in text or "小妾" in text:
        conflicts.append("身份冲突:女主处于低位身份,安全感依赖权力关系。")
    if "赵瑟瑟" in text:
        conflicts.append("情感与利益冲突:旧爱回归触发后宅和孩子危机。")
    if "孩子" in text or "林昭" in text:
        conflicts.append("亲子与归属冲突:孩子身世成为后半段推进核心。")
    return conflicts or ["待后续 LLM 抽取关键冲突。"]


def _hooks(text: str) -> list[str]:
    hooks: list[str] = []
    if "一脚把我踢下牛车" in text:
        hooks.append("开篇被家人抛弃,快速建立生存危机。")
    if "突然他的脑袋飞了" in text:
        hooks.append("极端危机中的救援登场,形成强钩子。")
    if "休书" in text and "家书" in text:
        hooks.append("误把家书当休书,制造信息误会和反转。")
    if "我是你爹" in text:
        hooks.append("父子相认段落具有强戏剧冲突和短视频切片潜力。")
    return hooks or ["待后续 LLM 抽取钩子、反转和爽点。"]


def _risks(text: str) -> list[str]:
    risks: list[str] = []
    if "糟蹋" in text or "军妓" in text:
        risks.append("开篇涉及性暴力和军妓表达,需要审核视角重点复核。")
    if "一刀砍成两段" in text or "脑袋飞了" in text:
        risks.append("暴力描写强,投放或平台展示时需要风险降级。")
    if "侍妾" in text or "小妾" in text:
        risks.append("身份关系和性别处境存在价值观解读风险。")
    return risks or ["D3 未识别到明显风险,后续需要 LLM 与人工评估补充。"]


def _next_step(text: str) -> str:
    if "赵瑟瑟" in text and "家书" in text:
        return "建议 D4 优先补证据定位,把旧爱回归、被赶出府、家书误会和父子相认四个节点做成可点击证据。"
    return "建议进入 D4,补充 evidence refs 和多轮问答。"
