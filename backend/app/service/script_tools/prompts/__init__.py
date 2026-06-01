"""rewrite_chain 用的 prompt 资源 + loader。

Layout (under this package):

    script_studio/
        plan/
            _system.zh.md
            _output_contract.zh.md
            by_dimension/
                hook.zh.md
                archetype.zh.md
                payoff.zh.md
                monetization.zh.md
                producibility.zh.md
        execute/
            _system.zh.md
            by_dimension/
                hook.zh.md  ... (5 dims)
        critic/
            plan_critic.zh.md

See ``prompt_loader.load_prompt`` for the rendering contract.
"""
