## Consulting (LLM second opinions)

The coding agent is not an expert for marketing, pricing, positioning, distribution, design or sales, and the
developer is not necessarily one either – so a commercial question does not wait for them.
Use `consult` (shared toolkit, `~/consult/CLAUDE.md` – read its "when to use what" table), configured for this
project in `.consult/config.toml`:

- `consult ask --label "<topic>" -q "…" --role "<invented role>"` for a guess while building; `consult panel …
  --gonogo "…"` (flagship, `--effort high`) only for decisions with money or that are hard to reverse, and before
  any stop/pivot; `consult review --pages …` for a page before sending traffic; `consult chat --thread <name>` to
  answer a consultant's follow-up questions.
- Trigger it yourself **before building**, the moment you notice you are guessing at something commercial.
- The personas are synthetic roles and a GO/NO GO is a recommendation: the person running the project decides.
  Reports land in the project's reviews directory with their cost; turn them into tasks, apply what you agree with,
  record what you rejected and why, and **say what the run cost**. Tag every number in a question measured / benchmark / guess.
  **Measured numbers outrank every consultant.** Record standing decisions with `consult memory add "…"`.
