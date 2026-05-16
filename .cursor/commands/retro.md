Review this conversation session and identify improvements for the reusable `.cursor` engineering package.

1. Look through the conversation for repeated mistakes or suboptimal patterns:
   - Did I violate core engineering constraints such as fail-fast errors, type hints, no silent fallbacks, or no ghost layers?
   - Did I violate ScriptLens boundaries across API, ingest, segmentation, evidence, perspectives, rewrite, feedback, evaluation, frontend, deployment, or docs?
   - Did I solve a one-off symptom instead of documenting a reusable invariant?
   - Did I miss a deterministic guardrail that should be enforced by a hook instead of relying on memory?
   - Did I create a workflow that should become a command instead of a rule?
   - Did I create complex domain guidance that should become a skill or a skill reference?
   - Did I add too much always-on context that should be scoped by globs or moved to a skill?

2. Classify each improvement into exactly one target:
   - `rule`: durable default behavior or file-scoped convention
   - `skill`: complex reusable workflow with references, examples, or scripts
   - `command`: manual slash workflow such as review, triage, release, or retro
   - `hook`: deterministic safety check, formatter, audit, or blocker
   - `docs`: human-facing explanation or technical article

3. Format the output as a review checklist:
   ```
   - [ ] Target: rule | skill | command | hook | docs
     File: .cursor/...
     Add/change: "specific text or behavior"
     Reason: what repeated failure this prevents
   ```

4. If no concrete improvement is justified, say so and avoid inventing new rules.
