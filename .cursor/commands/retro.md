Review this conversation session and identify improvements for cursor rules.

1. Look through the conversation for any mistakes or suboptimal patterns:
   - Did I generate overly defensive code (bare except, silent returns)?
   - Did I violate ScriptLens architecture boundaries (API, ingest, segmentation, reporting, LLM, frontend)?
   - Did I add features not explicitly requested?
   - Did I write redundant comments or Ghost Layer wrappers?
   - Did I miss type hints or use poor naming?
   - Did I introduce any security issues?
   - Did I drift away from `docs/source/task.md`?
   - Did I produce analysis, rewrite, or evaluation logic without evidence grounding?

2. For each issue found, propose a specific update to `.cursor/rules/`:
   - Which rule file to update (or create)
   - Exact text to add
   - Why this rule would have prevented the mistake

3. Format the output as a checklist I can review and apply:
   ```
   - [ ] File: .cursor/rules/xxx.mdc
     Add: "- specific rule text here"
     Reason: what mistake this prevents
   ```

4. If no mistakes were found, say so and suggest any general improvements
   based on patterns observed in this session.
