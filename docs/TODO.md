# Open work

From a `consult panel` of 2026-09-28 on making this repo public (the report stays local: NO GO until the fix list
is done, then GO "as a small experimental tool") – the tool reviewing its own release. Done the same day: LICENSE, honest
wording (synthetic roles, recommendation not decision), `init`/`status` warn when git tracks or does not ignore
the config or reviews directory, cost per call/report and a cost cap, the worked example.

## Before going public (done 2026-09-29)

- [x] **Quickstart without a secret manager**, tested in a clean environment (fresh clone, plain `.env`, `npm install`,
      one `ask` on a mini model); pin tested versions (Python, Node, Playwright) in the README.
      *2026-09-29: `python:3.11-slim` container, key exported (never written to disk), `init`, `status`, dry run,
      `ask` on gpt-6-luna ($0.0001); `npm ci --ignore-scripts` + `shots` from a fresh copy. The model setting is now
      required (no silent "newest" model); `init` defaults to English answers.*
- [x] **Dry-run preview**: `--dry-run` prints exactly what would be sent – context files (resolved symlink
      targets), memory, thread rounds, images, pages, model, effort, estimated input tokens – without calling
      the model. Route explicit `--files` and configured `context_files` through the same normalised `never_send`
      check. Explain in the README what the provider stores and what the reports keep.
      *2026-09-29: `--dry-run` on ask/chat/panel/review; one check for context files, `--files` and `--images`
      (path as given, normalised, and resolved symlink target); outside the project needs `--allow-outside`;
      `store: false` on every call; README "What leaves your machine, and what stays".*
- [x] **Offline tests** (no credentials, CI-safe): missing key, HTTP error, corrupt stream event, stall, a panel
      stopped by the cost cap, `ask --thread` round-trip, `init` warnings, pricing parser against a saved page.
      A failed or partial run must never look like a finished one.
      *2026-09-29: 32 tests (`tests/`), CI on 3.11/3.13. Fixed on the way: a stream cut off without a final event
      passed as a finished answer; corrupt events and connection errors gave tracebacks; a panel stopped by the cap
      or an error kept "in progress" – now marked ABORTED; `review` deleted its screenshots before sending them
      (reviews since the start sent page text only).*
- [x] **Web search and screenshots opt-in**: treat fetched page content and reports as data, not instructions;
      screenshots in a browser profile without cookies or keys; a lockfile for `node_modules`; document that
      `--no-web` removes only the search tool, not the model call.
      *2026-09-29: web search off unless `--web` or `CONSULT_WEB=on`; screenshots need the
      separate `npm ci` install; system prompt marks all read material as data; browser with a clean env, fresh
      context, no service workers, http(s) only, cross-host redirects fail; `package-lock.json` versioned.*
- [x] Check the history for private content before publishing (panel item 1).
      *2026-09-29: published with a fresh history. An outside run of the quickstart is left to the first users –
      please open an issue when a step does not work.*

## Nice to have

- [x] More providers behind the same `complete()` signature, pricing per provider.
      *2026-09-29: Anthropic (Messages API, adaptive thinking, effort, server-side web search, `pause_turn` resumed,
      refusal fallback) and OpenRouter (chat completions, `reasoning.effort`, `web` plugin, its reported cost);
      provider from the model name or `CONSULT_PROVIDER`; generic `CONSULT_*` settings. Offline tests only so far.*
- [ ] A live smoke run on Claude and on OpenRouter (one `ask` each on a small model), recorded here with its cost.
- [ ] `consult report --summary` that prints only the synthesis/verdict of a report for the person.
- [ ] Per-project monthly cost log (`consult cost --month`), summed from the reports' cost lines.
