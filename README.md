# consult

LLM second opinions for small projects: one-shot questions to a synthetic persona, panels of several personas with
a synthesis, page reviews from screenshots, and threaded chats with memory – with each project's goal, numbers and
personas attached, and the cost of every run recorded. Built for AI agents (Claude Code) to call from inside any
project; humans can use it the same way. The personas are roles played by one model, not independent experts; a
GO/NO GO is a recommendation for the person who decides. Works with **OpenAI**, **Anthropic (Claude)** or
**OpenRouter** – streamed, with stall detection and optional web search, no SDKs.

```
consult status
consult ask --label "first signups" -q "We have 2 ad clicks and 0 sign-ups …" --persona growth
consult panel --label "budget" -q "…" --personas growth,cfo,skeptic --gonogo "Raise the ad budget to 20 €/day?"
consult review --pages /de/ /es/ --gonogo "Send paid traffic to this page?"
consult chat --thread budget -q "Here are the numbers you asked for …" --images shot.png
consult panel … --dry-run      # print exactly what would be sent, and an input estimate – no model call
```

## Requirements

- **Python 3.11+**, standard library only (no pip install). Tested with 3.11 and 3.13.
- An API key for **one** provider and a model name (`CONSULT_MODEL`; nothing is picked for you) – see Providers.
- *Optional, only for screenshots* (`review`, `shots`, `--pages`): Node 20+, Playwright 1.63.0 (pinned in
  `package-lock.json`) and its Chromium. Without them, `--pages` attaches the page text only.

Tested on 2026-09-29: a clean `python:3.11-slim` container (core: `status`, a dry run, one `ask`), Debian 13 with
Python 3.13.5, Node 20.19.2, npm 9.2 (screenshots).

## Quickstart (about 5 minutes)

```
git clone https://github.com/Kipperlenny/consult.git ~/consult
cp ~/consult/.env.example ~/consult/.env        # set one API key + CONSULT_MODEL – or export them instead
mkdir -p ~/.local/bin && ln -s ~/consult/bin/consult ~/.local/bin/consult     # or call ~/consult/bin/consult

cd ~/my-project
consult init                  # --private for a public repo: config and reports go to private/ – which must be ignored
consult status                # key found? model? cost cap? does git track the reports directory?
consult ask --label smoke -q "Is a free newsletter for dog owners a business? One paragraph." \
    --model gpt-6-luna --effort low --dry-run   # see what would be sent
consult ask --label smoke -q "Is a free newsletter for dog owners a business? One paragraph." \
    --model gpt-6-luna --effort low             # a fraction of a cent; the report lands in .consult/reviews/
                                                # (claude-haiku-4-5 or openai/gpt-6-luna work the same way)
```

Then fill in `.consult/config.toml` (what the project is, its goal, which files every consultant sees, what must
never be sent) and paste `consult snippet` into the project's `CLAUDE.md` so its agents know when to ask.

**Screenshots (optional):**

```
cd ~/consult && npm ci --ignore-scripts && npx playwright install chromium   # Linux may need: --with-deps
consult shots --pages /       # desktop + phone JPEGs of project.site
```

`npm ci` installs exactly the locked versions; `--ignore-scripts` skips install scripts, which is not a sandbox for
code you run later. The browser runs with a clean environment (no API keys), a fresh context per viewport (no
cookies, no profile, no service workers), http(s) only, and a redirect to another host fails the shot.

## Providers

The model name picks the provider; `CONSULT_PROVIDER=openai|anthropic|openrouter` overrides it.

| Model name | Provider | Key | Web search (`--web`) | Cost figure |
|---|---|---|---|---|
| `gpt-…`, `o…` | OpenAI Responses API (`OPENAI_API=chat`: chat completions, no tools) | `OPENAI_API_KEY` | built-in `web_search` tool | estimate from the pricing page |
| `claude-…` | Anthropic Messages API: adaptive thinking, `--effort` → `output_config.effort` | `ANTHROPIC_API_KEY` | server-side `web_search` tool | estimate from a dated price table |
| `vendor/model` (`openai/gpt-6-sol`, `anthropic/claude-opus-5-5`, …) | OpenRouter chat completions: `--effort` → `reasoning.effort` | `OPENROUTER_API_KEY` | `web` plugin | the real cost OpenRouter reports |

Mixing works: an everyday model from one provider (`CONSULT_MODEL`) and the "best" one from another
(`CONSULT_MODEL_BEST`), or `--model` per call. Older `OPENAI_MODEL`, `OPENAI_MODEL_BEST`, `OPENAI_EFFORT` and
`OPENAI_STALL_SECONDS` settings still work. Claude models that support it get Anthropic's server-side refusal fallback
(`fallbacks: "default"`, beta): if a safety classifier declines, another Claude model answers in the same call and
the report says which one. A refusal that is not rescued is an error, never a saved answer.

**Provider fallback.** With `CONSULT_FALLBACK_MODEL` (and optionally `CONSULT_FALLBACK_MODEL_BEST` for the best model)
set, a call whose provider is unavailable – no key, a rejected key (401/403), or no credit left (Anthropic's "credit
balance is too low", OpenAI's `insufficient_quota`) – is asked once more on the fallback model, if that one's key is
set. stderr says so and the report names the model that answered. Other errors (overload, timeout, a cut-off stream)
never fall back. `CONSULT_PROVIDER` forces both onto one provider, so leave it empty when you use a fallback.

## What leaves your machine, and what stays

Sent to the model provider (OpenAI, Anthropic, or OpenRouter and the provider it routes to) with every call: the
system prompt (persona + the project's `framing`), the project memory, the context files, the question, `--pages`
text and screenshots, `--images`, and in a thread every earlier round. `--dry-run` prints all of it with each file's
resolved path, without sending. OpenAI calls are made with `store: false` (not kept for later retrieval); each
provider's own retention still applies – check its current data-controls documentation. With web search on (`--web`
or `CONSULT_WEB=on`), the model also writes search queries that can be derived from your context, and reads what it
finds; `--no-web` removes only that tool, not the model call.

Guards, and their limits: files and images pass one check – the path as given and its resolved symlink target must not
match the project's `never_send` list, and anything outside the project needs `--allow-outside`. This matches
**paths, not content**: a secret pasted into a question or visible in a screenshot goes out. Everything a consultant
reads – pages, search results, earlier reports – is marked in the prompt as material, not instructions.

Kept locally, in the project's `reviews_dir`: every report (model, effort, context file *names*, system prompt,
question, answers, tokens, cost estimate), screenshots under `shots/`, and threads as JSON under `threads/` – these
hold the **full text** of every round, including the context block. Keep `reviews_dir` out of any public repository;
`consult init` and `consult status` warn when git tracks it or does not ignore it.

## Costs

Every call prints and records its cost: for OpenAI list prices (parsed from the pricing page, cached a week), for
Claude a dated table in `consult/pricing.py`, each × the tokens the API reports, including hidden reasoning, plus
web-search calls; for OpenRouter the cost it reports per call. `panel` and `review` stop when the estimate
passes `--max-usd` (default `CONSULT_MAX_USD`, 5); a run that stops early or fails is marked **ABORTED** in its
report, never left looking finished. `consult prices`, `consult cost <report.md>`.

## Tests

`python3 -m unittest discover -s tests` – offline, no key, no network, no browser: missing key, HTTP errors, corrupt
or cut-off streams, stalls, Claude's paused and refused turns, OpenRouter's reported cost, panels stopped by the cost cap or a failing call, thread round-trips, `never_send` with
symlinks and `../`, the dry run, `init` warnings, and the pricing parser against a saved excerpt of the page. CI runs
them on Python 3.11 and 3.13.

## Honest limits

- One model, several prompts: contradictions between personas are useful, agreement is not proof.
- `never_send` matches paths, not content. Everything you attach goes to the model provider.
- Costs are estimates from list prices × reported tokens (OpenRouter: its own figure).
- The OpenAI path is the one used most; Claude and OpenRouter are covered by the offline tests against the
  documented stream formats – report anything that behaves differently live.
- A worked example with real output and cost: [docs/example-consultation.md](docs/example-consultation.md).
  Open work before this is more than an experiment: [docs/TODO.md](docs/TODO.md).

## Layout

```
consult/        the Python package (stdlib only): cli, config, personas, providers, context, report, pricing, commands
personas/       reusable experts (TOML): growth, conversion, design, skeptic, privacy-critic, cfo, media-buyer,
                seo, copywriter, pricing, b2b-sales, community, product, lawyer-eu, first-user, pre-mortem, steelman
tools/          shots.cjs (Playwright screenshots, desktop + phone)
tests/          offline tests; fixtures/pricing-excerpt.html
docs/           project-snippet.md (paste into a project's CLAUDE.md), example-consultation.md, TODO.md
```

Details for agents and all options: [CLAUDE.md](CLAUDE.md). `.env` may be a plain file or a symlink from a secret
manager; the process environment always wins over it.
