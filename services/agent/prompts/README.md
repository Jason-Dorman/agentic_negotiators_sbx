# Agent prompt templates

The model policy's prompts, versioned and changed through review like any other source
([ADR-029](../../../docs/decision_log.md)). Each directory is one template version:

- `system.md` is the system prompt, rendered once per run and sent behind a cache breakpoint. Its
  order is fixed: the role, the protocol rules, the output schema, then the mandate's
  `instructions` in a delimited section introduced as the agent's own private guidance.
- `repair.md` is the second text block of a repair attempt's user message. It carries this agent's
  own validation code and feedback for the attempt before, and nothing else.

Placeholders are `${name}` (Python's `string.Template`). A value is never scanned for placeholders
itself, so a mandate's `instructions` cannot reach into the template.

The version recorded on every decision, `prompt_template_version`, is the directory name and a hash
of both files and the decision schema the prompt embeds, for example `v1.0.0+3f9a0c1e2b4d5a6f`. A
change to a file without a new directory still changes the hash, so two decisions with the same
recorded version were always made from the same text.

`agent.prompting` loads the templates. The wheel carries this directory as `agent/_prompts`
(`pyproject.toml`); in the source tree it is read from here.
