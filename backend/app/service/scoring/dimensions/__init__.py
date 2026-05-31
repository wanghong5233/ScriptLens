"""scoring v4 dimensions。

每个 dimension 暴露：
- async score_dimension(ctx: ScoringContext, dim_cfg: DimensionConfig) -> DimensionScore

调用入口由 scoring.main_chain 统一调度。
"""

from service.scoring.dimensions.archetype import score_dimension as score_archetype
from service.scoring.dimensions.hook import score_dimension as score_hook
from service.scoring.dimensions.monetization import score_dimension as score_monetization
from service.scoring.dimensions.payoff import score_dimension as score_payoff
from service.scoring.dimensions.producibility import score_dimension as score_producibility

DIMENSION_FUNCS = {
    "hook": score_hook,
    "archetype": score_archetype,
    "payoff": score_payoff,
    "monetization": score_monetization,
    "producibility": score_producibility,
}

__all__ = [
    "DIMENSION_FUNCS",
    "score_archetype",
    "score_hook",
    "score_monetization",
    "score_payoff",
    "score_producibility",
]
