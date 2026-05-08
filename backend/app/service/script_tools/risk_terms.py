"""审核风险关键词词表（广电六类红线）。

按 rubric §3.5 的 4 档分级（high_risk / medium_risk / low_risk / clean），
关键词层只负责"召回"，每个命中都丢给 LLM 二级判定（防止"杀人"在比喻里
被误判）。

来源：广电总局《关于进一步加强网络微短剧管理 实施创作提升计划有关工作的通知》
(2022.12) 列出的"导向关 / 内容关 / 审美关"重点风险，扩展自抖音 / 快手平台
2024 内部审核 SOP 公开摘要。
"""

from __future__ import annotations

from typing import Dict, List


# ============================================================
# high_risk —— 红线，命中即视为不可发布（除非二级判定排除误命中）
# ============================================================

HIGH_RISK_TERMS: Dict[str, List[str]] = {
    "underage_sexual": [
        "未成年", "未成年人", "高中生", "初中生", "小学生", "童养媳",
        "幼女", "处女", "少女初", "破处",
    ],
    "drugs_method": [
        "制毒", "贩毒", "毒品配方", "麻黄碱", "冰毒", "海洛因",
        "毒品交易",
    ],
    "suicide_method": [
        "上吊", "割腕", "跳楼自杀", "服毒自杀", "煤气自杀", "自杀方法",
    ],
    "ethnic_political": [
        "藏独", "疆独", "台独", "港独", "新疆问题",
        "敏感民族", "种族优劣", "宗教对立",
    ],
}


# ============================================================
# medium_risk —— 主流题材风险，主线含此类元素降级
# ============================================================

MEDIUM_RISK_TERMS: Dict[str, List[str]] = {
    "wealth_worship": [
        "穷鬼", "穷酸", "穷光蛋", "土包子",
        "10亿", "百亿", "千亿", "全款买", "包养",
        "首富儿子", "富二代追", "豪门弃妇",
    ],
    "violence_chain": [
        "暴打", "毒打", "群殴", "刑讯", "鞭打", "酷刑",
        "断手", "断腿", "活埋", "肢解",
    ],
    "ethics_dispute": [
        "小三上位", "原配让位", "代孕", "出轨", "插足", "私生子",
        "假死", "换子", "认贼作父",
    ],
    "medical_misconduct": [
        "假药", "祖传秘方包治", "神医", "起死回生",
        "代孕生子", "黑市买肾",
    ],
    "violent_revenge": [
        "灭门", "斩草除根", "全家陪葬", "血洗",
        "断子绝孙",
    ],
}


# ============================================================
# low_risk —— 局部出现可接受，集中出现降档
# ============================================================

LOW_RISK_TERMS: Dict[str, List[str]] = {
    "vulgar_language": [
        "贱人", "贱货", "婊子", "废物", "草泥", "傻逼", "滚蛋",
        "去死", "畜生",
    ],
    "innuendo": [
        "壁咚", "强吻", "扑倒", "压在身下", "解开扣子",
        "撕碎衣服", "睡了她", "睡了他",
    ],
    "minor_violence": [
        "扇耳光", "推搡", "踢倒", "拳头",
    ],
}


# ============================================================
# 工具：扁平化 / 反查
# ============================================================


def all_high_risk_terms() -> List[str]:
    return _flatten(HIGH_RISK_TERMS)


def all_medium_risk_terms() -> List[str]:
    return _flatten(MEDIUM_RISK_TERMS)


def all_low_risk_terms() -> List[str]:
    return _flatten(LOW_RISK_TERMS)


def categorize_term(term: str) -> tuple[str, str] | None:
    """反查关键词所属 (level, category)。命中多个时 high > medium > low。"""
    for cat, terms in HIGH_RISK_TERMS.items():
        if term in terms:
            return "high_risk", cat
    for cat, terms in MEDIUM_RISK_TERMS.items():
        if term in terms:
            return "medium_risk", cat
    for cat, terms in LOW_RISK_TERMS.items():
        if term in terms:
            return "low_risk", cat
    return None


def _flatten(d: Dict[str, List[str]]) -> List[str]:
    out: List[str] = []
    for terms in d.values():
        out.extend(terms)
    return out


# ============================================================
# reward 事件关键词（reward_extractor 用，与 risk 同源管理）
# ============================================================

REWARD_TERMS: Dict[str, List[str]] = {
    "face_slap": [  # 打脸
        "啪的一声", "扇了一耳光", "甩了一巴掌", "啪", "扇耳光", "重重一巴掌",
        "脸都肿了",
    ],
    "reversal": [  # 反转 / 真相揭露
        "真相", "原来", "竟然是", "其实是", "没想到", "万万没想到",
        "身份揭露", "假身份", "假装",
    ],
    "revenge": [  # 复仇成功
        "报仇", "复仇", "终于", "亲手", "破产", "下跪", "认错",
        "求饶", "悔不当初",
    ],
    "romantic_progress": [  # CP / 关系突破
        "亲了", "吻了", "抱住", "求婚", "结婚", "表白", "在一起",
        "我喜欢你", "我爱你", "做我女朋友", "做我老婆",
    ],
    "identity_reveal": [  # 身份揭露
        "总裁", "首富", "影帝", "公主", "王爷", "继承人",
        "马甲", "vip", "金主",
    ],
    "humiliate_villain": [  # 反派败落
        "众叛亲离", "身败名裂", "锒铛入狱", "判刑", "自食恶果",
    ],
    "underdog_rise": [  # 逆袭
        "逆袭", "翻身", "崛起", "从此", "时来运转",
    ],
    "scheme_exposed": [  # 阴谋败露
        "阴谋", "诡计", "败露", "戳穿", "拆穿", "露馅",
    ],
}


def all_reward_terms() -> List[str]:
    return _flatten(REWARD_TERMS)


def categorize_reward_term(term: str) -> str | None:
    for cat, terms in REWARD_TERMS.items():
        if term in terms:
            return cat
    return None


# ============================================================
# HOOK 关键词（开场冲突 / 题材标识共用）
#
# 业内对照（抖音文心 / 快手短剧选品 SOP）：「钩子事件」= 让用户在前 30
# 秒内识别"这是哪个赛道的剧 + 主角处境是不是足够极端"的事件锚点。
# 信号上对 v1 dimension_scorer 中 _OPENING_CONFLICT_KEYWORDS 与
# _CONCEPT_KEYWORDS 两套字面重复词表的合并整理（DRY）。
#
# 用法：
# - score_pacing 用 HOOK_KEYWORDS 检测首场是否快速进入冲突
# - score_concept 用 HOOK_KEYWORDS 检测首集前 3 场是否出现题材标识
# - 不再单独声明 _OPENING_CONFLICT_KEYWORDS / _CONCEPT_KEYWORDS
# ============================================================

HOOK_KEYWORDS: tuple[str, ...] = (
    "死", "死亡", "绝症",
    "离婚", "出轨", "退婚", "分手", "插足", "原配",
    "重生", "穿越", "复仇", "报仇",
    "当众", "羞辱", "被骂", "扫地出门",
    "阴谋", "真相", "误会", "反目", "重逢", "追妻", "认亲", "翻身",
    "打", "推倒", "巴掌", "扇耳光",
)


# ============================================================
# 主流赛道白名单（短剧选品标签）
#
# 用途：
# - score_concept 用此名单判断 coverage_card.genre 是否落到主流赛道
# - 未来 coverage_chain / agent_runtime 若需做"赛道是否合规"校验可复用
#
# 来源：抖音 / 快手短剧 2024 选品 SOP 公开摘要 + 阅文短剧合作方分类。
# 任何模块需扩展赛道时，**改这一处**，不许在调用点写 inline 集合。
# ============================================================

MAINSTREAM_GENRES: frozenset[str] = frozenset({
    "重生", "穿越", "复仇", "战神", "豪门", "甜宠", "逆袭", "战神归来",
    "都市重生", "总裁", "替身", "弃妇", "扮猪吃虎", "马甲", "认亲",
    "古言", "现言", "玄幻", "悬疑", "权谋",
})
