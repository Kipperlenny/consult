"""The commands: ask, chat, panel, review, shots, personas, memory, init, status."""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import personas as personas_mod
from . import pricing
from .config import ROOT, ConsultError, Persona, Project, env, find_project, setting
from .context import (GENERIC_ROLE, check_images, check_url, context_block, first_message, pages_block, system_prompt,
                      today)
from .providers import api_key, complete, describe, resolve_effort, resolve_model, user_message
from .report import header, quote, report_path, slug, sum_usage, tokens_line

SYNTHESIS_ROLE = ("the project lead who turns several separately generated opinions into one prioritised action list – "
                  "a recommendation for the human who decides, not a decision")


# ---------- shared ----------

def _persona_for(project: Project, args) -> Persona:
    if getattr(args, "persona", None):
        return personas_mod.resolve(project, [args.persona])[0]
    return personas_mod.ad_hoc(args.role or project.default_role or GENERIC_ROLE)


def _model(project: Project, args) -> tuple[str, str]:
    effort = resolve_effort(project, args.effort)
    return resolve_model(project, args.model, effort=effort, best=getattr(args, "best", False)), effort


def _web(args) -> bool:
    """Web search is opt-in: --web, or CONSULT_WEB=on as the default in .env; --no-web wins."""
    if getattr(args, "no_web", False):
        return False
    return bool(getattr(args, "web", False)) or (env("CONSULT_WEB") or "").lower() in ("1", "on", "true", "yes")


def _outside(args) -> bool:
    return bool(getattr(args, "allow_outside", False))


def _pages(project: Project, args) -> tuple[str, list[Path]]:
    """--pages on ask/chat/panel: the pages' visible text (always) and screenshots (if node works)."""
    pages = getattr(args, "pages", None)
    if not pages:
        return "", []
    base = (getattr(args, "base", None) or project.site).rstrip("/")
    if not base and any("://" not in p for p in pages):
        raise ConsultError("--pages needs project.site or --base, or full URLs")
    urls = [check_url(p if "://" in p else base + p) for p in pages]
    shots = []
    try:
        shots = take_shots(urls, project.reviews_dir / "shots" / time.strftime("%Y-%m-%d-%H%M%S"))
    except ConsultError as e:
        print(f"  (no screenshots: {e})", file=sys.stderr)
    return pages_block(urls), shots


def _files(project: Project, args) -> list[str]:
    return list(args.files) if args.files is not None else list(project.context_files)


def _meta(project: Project, model: str, effort: str, files: list[str], **extra) -> dict:
    return {"Project": project.name, "Model": model, "Effort": effort, "Context files": ", ".join(files) or "none",
            **extra, "Generated with": "`consult`"}


def _usage_extra(usage: dict) -> dict:
    return {"Web searches": usage.get("web_searches", 0)}


def _max_usd(args) -> float:
    return float(args.max_usd if getattr(args, "max_usd", None) is not None else env("CONSULT_MAX_USD", "5"))


def _check_budget(usages: list[dict], limit: float, out: Path):
    """Stop a multi-call run when the estimated spend passes the cap; the partial report is already on disk."""
    spent = sum_usage(usages).get("cost_usd") or 0
    if spent > limit:
        raise ConsultError(f"stopped: estimated cost ${spent:.2f} exceeds the cap of ${limit:.2f} "
                           f"(--max-usd / CONSULT_MAX_USD). Partial report: {out}")


def _git_tracked(root: Path, path: Path) -> list[str]:
    """Files under `path` that git already tracks – a `private/` name is not a guard."""
    try:
        r = subprocess.run(["git", "-C", str(root), "ls-files", "--", str(path)], capture_output=True, text=True)
    except FileNotFoundError:
        return []
    return [line for line in r.stdout.splitlines() if line]


def _git_ignored(root: Path, path: Path) -> bool | None:
    try:
        r = subprocess.run(["git", "-C", str(root), "check-ignore", "-q", str(path)], capture_output=True)
    except FileNotFoundError:
        return None
    return r.returncode == 0 if r.returncode in (0, 1) else None


IMAGE_TOKENS_GUESS = 1_500  # one high-detail screenshot; the real figure depends on size and model


def _dry_run(project: Project, model: str, effort: str, args, calls: list[tuple[str, str]], user_text: str,
             images: list[Path], sources: list[tuple[str, str, int]], note: str = ""):
    """Print exactly what a run would send – without calling the model or writing a report or thread."""
    web = _web(args)
    print(f"DRY RUN – nothing is sent, no report or thread is written.\n"
          f"provider:   {describe(model)}\n"
          f"model:      {model}, effort {effort}, max output tokens {args.max_tokens}\n"
          f"web search: {'ON – the model may search and open pages itself' if web else 'off'}\n"
          f"project:    {project.name} ({project.config_path or 'no config'}); never_send: "
          f"{', '.join(project.never_send) or '-'}")
    print("sent with every call:")
    for kind, name, chars in sources:
        print(f"  {kind:<8} {name}  ({chars:,} chars)")
    for img in images:
        print(f"  {'image':<8} {img}  " + (f"({img.stat().st_size // 1024:,} kB)" if img.is_file() else "(missing – "
                                                 "sent as a note that it no longer exists)"))
    if note:
        print(f"  {note}")
    per_call = len(user_text) // 4 + len(images) * IMAGE_TOKENS_GUESS
    total = sum(len(system) // 4 + per_call for _, system in calls)
    data = pricing.load()
    floor = pricing.cost(model, {"prompt_tokens": total}, data) if data else None
    print(f"calls:      {len(calls)}" + (" + 1 synthesis (its input is the answers)" if len(calls) > 1 else "")
          + f"\nestimated:  ≈ {total:,} input tokens (chars/4, {IMAGE_TOKENS_GUESS:,} per image – a guess); input "
          f"alone ≈ {pricing.fmt(floor)} at list prices, plus output and hidden reasoning (usually more)")
    for i, (name, system) in enumerate(calls, 1):
        print(f"\n----- call {i}/{len(calls)}: {name} – system prompt -----\n{system}")
    print(f"\n----- user message{' (the same for every call)' if len(calls) > 1 else ''} -----\n{user_text}")


def _run_experts(project: Project, experts: list[Persona], out: Path, head: str, messages_for, model: str,
                 effort: str, args) -> None:
    """Each expert blind to the others, then the synthesis. The report is rewritten after every expert; a run that
    stops early (cost cap, error, Ctrl-C) is marked ABORTED so it can never pass for a finished report."""
    opinions, usages, sections = {}, [], []
    try:
        for i, e in enumerate(experts, 1):  # independent blind spots are the point
            print(f"[{i}/{len(experts)}] {e.name}:", file=sys.stderr)
            text, usage, _ = complete(messages_for(e), model, effort, args.max_tokens, args.stall, web=_web(args))
            opinions[e.name] = text
            usages.append(usage)
            sections.append(f"## {e.name} — {e.title}\n\n{text.strip()}\n\n{tokens_line(usage)}\n")
            out.write_text(head + "\n".join(sections) + "\n*[in progress: synthesis pending]*\n", encoding="utf-8")
            _check_budget(usages, _max_usd(args), out)
        synth, usage = _synthesis(project, model, effort, opinions, args.gonogo, args.max_tokens, args.stall)
    except (ConsultError, KeyboardInterrupt) as e:
        reason = "interrupted (Ctrl-C)" if isinstance(e, KeyboardInterrupt) else str(e)
        out.write_text(head + "\n".join(sections)
                       + f"\n## ABORTED – not a finished report\n\n*{len(sections)} of {len(experts)} experts "
                       f"answered; no synthesis. Reason: {reason}*\n\n"
                       + (tokens_line(sum_usage(usages)) + "\n" if usages else ""), encoding="utf-8")
        raise
    usages.append(usage)
    out.write_text(head + "\n".join(sections)
                   + f"\n## Synthesis — prioritised actions (a recommendation; the human decides)\n\n{synth.strip()}\n\n"
                   + tokens_line(sum_usage(usages)) + "\n", encoding="utf-8")
    print(f"wrote {out}  ({pricing.fmt(sum_usage(usages).get('cost_usd'))} estimated)")


def _synthesis(project: Project, model: str, effort: str, opinions: dict[str, str], gonogo: str | None,
               max_tokens: int, stall: int | None) -> tuple[str, dict]:
    system = (f"You are: {SYNTHESIS_ROLE}. Summarise the opinions into one prioritised action list: at most 10 "
              f"items, sorted by expected effect on {project.kpi} divided by effort. Each item: one line What, one "
              "line How exactly, one line Why (which expert). Mark contradictions between the experts explicitly. "
              "Then: 3 things the experts praise that should stay. "
              f"Answer in {project.language_name()}, Markdown.")
    if gonogo:
        system += (f" Additionally, at the very top, a recommendation on this question: '{gonogo}' – answer with "
                   "**GO** or **NO GO**, justify in 5 sentences, and for NO GO list the minimum that must change first "
                   "(only what really endangers the measure's success, not everything desirable). Make clear it is a "
                   "recommendation; the person running the project decides.")
    user = "\n\n".join(f"### {name}\n{text}" for name, text in opinions.items())
    print("synthesis:", file=sys.stderr)
    text, usage, _ = complete([{"role": "system", "content": system}, {"role": "user", "content": user}],
                              model, effort, max_tokens=min(max_tokens, 24_000), stall=stall, web=False)
    return text, usage


# ---------- ask ----------

def cmd_ask(args):
    if args.thread:
        args.show = False  # `chat`-only flag
        return cmd_chat(args)
    project = find_project()
    persona = _persona_for(project, args)
    model, effort = _model(project, args)
    files = _files(project, args)
    system = system_prompt(project, persona, dialog=args.dialog, override=args.system)
    pages, shots = _pages(project, args)
    ctx = context_block(project, files, note=pages, allow_outside=_outside(args))
    used = ctx.files
    images = check_images(project, args.images, _outside(args)) + shots
    user_text = first_message(ctx.text, args.question)
    if args.dry_run:
        return _dry_run(project, model, effort, args, [(persona.name, system)], user_text, images, ctx.sources)
    messages = [{"role": "system", "content": system}, user_message(user_text, images)]
    print(f"asking {model} as {persona.name} ({persona.title[:60]}…)" if len(persona.title) > 60
          else f"asking {model} as {persona.name} ({persona.title})", file=sys.stderr)
    text, usage, _ = complete(messages, model, effort, args.max_tokens, args.stall, web=_web(args))
    out = report_path(project, "consult", args.label, today())
    out.write_text(header(f"Consultation: {args.label} ({today()})",
                          _meta(project, model, effort, used, Persona=persona.name,
                                Pages=", ".join(args.pages or []), Images=", ".join(i.name for i in images),
                                **_usage_extra(usage)))
                   + f"*System prompt:*\n\n{quote(system)}\n\n*Question:*\n\n{quote(args.question)}\n\n{text}\n\n"
                   + tokens_line(usage) + "\n", encoding="utf-8")
    print(f"wrote {out}")


# ---------- chat (threads with memory) ----------

def _thread_path(project: Project, name: str) -> Path:
    if "/" in name or name.endswith(".json"):
        return Path(name).expanduser().resolve()
    project.threads_dir.mkdir(parents=True, exist_ok=True)
    return project.threads_dir / f"{slug(name)}.json"


def cmd_chat(args):
    project = find_project()
    path = _thread_path(project, args.thread)
    state = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
    if args.show:
        if not state:
            raise ConsultError(f"no thread at {path}")
        for i, r in enumerate(state["rounds"], 1):
            print(f"## Round {i} ({r.get('date', '?')})\n> {r['user'][:300]}…\n\n{(r.get('assistant') or '')[:1500]}…\n")
        print(f"report: {state['report']}")
        return
    if not args.question:
        raise ConsultError("--question is required (or --show to read the thread)")
    model, effort = _model(project, args)
    pages, shots = _pages(project, args)
    images = check_images(project, args.images, _outside(args)) + shots
    sources = []
    if state is None:
        persona = _persona_for(project, args)
        files = _files(project, args)
        system = system_prompt(project, persona, dialog=True, override=args.system)
        ctx = context_block(project, files, note=pages, allow_outside=_outside(args))
        sources = ctx.sources
        report = report_path(project, "consult", args.label or args.thread, today())
        state = {"label": args.label or args.thread, "persona": persona.name, "system": system, "files": ctx.files,
                 "report": str(report), "rounds": []}
        user_text = first_message(ctx.text, args.question)
    else:
        system, report = state["system"], Path(state["report"])
        user_text = f"{pages}\n\n{args.question}" if pages else args.question
    if args.dry_run:
        earlier = state["rounds"]
        prior = "".join(f"\n----- round {i}: sent -----\n{r['user']}\n----- round {i}: answer -----\n{r['assistant']}\n"
                        for i, r in enumerate(earlier, 1))
        return _dry_run(project, model, effort, args, [(f"thread {path.stem}, round {len(earlier) + 1}", system)],
                        prior + ("\n----- this round -----\n" if earlier else "") + user_text,
                        [Path(i) for r in earlier for i in r["images"]] + images, sources,
                        note=f"thread    {len(earlier)} earlier round(s) are re-sent in full ({path})")
    state["rounds"].append({"date": today(), "user": user_text, "images": [str(i) for i in images], "assistant": None})
    messages = [{"role": "system", "content": system}]
    for r in state["rounds"]:
        messages.append(user_message(r["user"], [Path(i) for i in r["images"]]))
        if r.get("assistant"):
            messages.append({"role": "assistant", "content": r["assistant"]})
    print(f"thread {path.stem}, round {len(state['rounds'])}, {model}", file=sys.stderr)
    text, usage, _ = complete(messages, model, effort, args.max_tokens, args.stall, web=_web(args))
    state["rounds"][-1]["assistant"] = text
    path.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    n = len(state["rounds"])
    shots = "".join(f"\n- `{Path(i).name}`" for i in state["rounds"][-1]["images"])
    if n == 1 or not report.is_file():
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(header(f"Consultation (thread): {state['label']} ({today()})",
                                 _meta(project, model, effort, state["files"], Persona=state["persona"],
                                       Thread=str(path)))
                          + f"*System prompt:*\n\n{quote(system)}\n\n## Round 1\n\n*Question:*\n\n"
                          + quote(args.question) + (f"\n\n*Screenshots:*{shots}" if shots else "")
                          + f"\n\n*Consultant:*\n\n{text}\n\n{tokens_line(usage)}\n", encoding="utf-8")
    else:
        with report.open("a", encoding="utf-8") as f:
            f.write(f"\n\n---\n\n## Round {n} ({today()})\n\n*Agent:*\n\n{quote(args.question)}"
                    + (f"\n\n*Screenshots:*{shots}" if shots else "") + f"\n\n*Consultant:*\n\n{text}\n\n{tokens_line(usage)}\n")
    print(f"round {n} saved to {report} (thread: {path})")


# ---------- panel ----------

def cmd_panel(args):
    project = find_project()
    names = args.personas.split(",") if args.personas else project.panel_personas
    if not names and not args.roles:
        raise ConsultError("no personas: pass --personas a,b,c or --roles '…' '…', or set panel_personas in the config")
    experts = personas_mod.resolve(project, [n.strip() for n in names if n.strip()])
    experts += [Persona(f"role{i + 1}", r, source="--roles") for i, r in enumerate(args.roles or [])]
    model, effort = _model(project, args)
    files = _files(project, args)
    pages, shots = _pages(project, args)
    ctx = context_block(project, files, note=pages, allow_outside=_outside(args))
    images = check_images(project, args.images, _outside(args)) + shots
    user_text = first_message(ctx.text, args.question)
    systems = {e.name: system_prompt(project, e, screenshots=bool(images)) for e in experts}
    if args.dry_run:
        return _dry_run(project, model, effort, args, list(systems.items()), user_text, images, ctx.sources)
    out = report_path(project, "panel", args.label, today())
    head = (header(f"Panel: {args.label} ({today()})",
                   _meta(project, model, effort, ctx.files, Experts=", ".join(e.name for e in experts)))
            + f"*Question:*\n\n{quote(args.question)}\n\n")
    _run_experts(project, experts, out, head,
                 lambda e: [{"role": "system", "content": systems[e.name]}, user_message(user_text, images)],
                 model, effort, args)


# ---------- screenshots + review ----------

def _node() -> str:
    if shutil.which("node"):
        return "node"
    versions = sorted((Path.home() / ".nvm/versions/node").glob("v*"), key=lambda p: [int(x) for x in p.name[1:].split(".")])
    if versions:
        return str(versions[-1] / "bin/node")
    raise ConsultError("node not found (needed for screenshots) – install Node or run `nvm install 24`")


def take_shots(urls: list[str], out: Path) -> list[Path]:
    if not (ROOT / "node_modules" / "playwright").is_dir():
        raise ConsultError(f"Playwright missing – run `npm install` in {ROOT}")
    out.mkdir(parents=True, exist_ok=True)
    # a clean environment: the browser process never sees API keys or other secrets from ours
    keep = ("PATH", "HOME", "LANG", "TMPDIR", "PLAYWRIGHT_BROWSERS_PATH", "XDG_CACHE_HOME")
    r = subprocess.run([_node(), str(ROOT / "tools" / "shots.cjs"), str(out), *urls], cwd=ROOT,
                       capture_output=True, text=True, env={k: os.environ[k] for k in keep if k in os.environ})
    if r.returncode:
        raise ConsultError(f"screenshots failed: {r.stderr.strip() or r.stdout.strip()}")
    return sorted(out.glob("*.jpg"))


def _urls(project: Project, args) -> tuple[str, list[str]]:
    base = (args.base or (f"https://{args.site}" if args.site else project.site)).rstrip("/")
    if not base:
        raise ConsultError("no site: pass --base https://… or --site host, or set project.site in the config")
    return base, [base + p for p in args.pages]


def cmd_shots(args):
    project = find_project()
    base, urls = _urls(project, args)
    out = Path(args.out) if args.out else project.reviews_dir / "shots" / time.strftime("%Y-%m-%d-%H%M%S")
    for p in take_shots(urls, out):
        print(p)


def cmd_review(args):
    project = find_project()
    base, urls = _urls(project, args)
    names = args.personas.split(",") if args.personas else project.review_personas
    if not names:
        raise ConsultError("no personas: pass --personas a,b,c or set review_personas in the config")
    experts = personas_mod.resolve(project, [n.strip() for n in names if n.strip()])
    model, effort = _model(project, args)
    for u in urls:
        check_url(u)
    shots_dir = Path(tempfile.mkdtemp(prefix="consult-review-"))
    print(f"screenshots of {len(urls)} page(s)…", file=sys.stderr)
    images = take_shots(urls, shots_dir)
    files = _files(project, args)
    note = f"Website: {base}. Pages in the screenshots: {', '.join(urls)}.\n\n" + pages_block(urls)
    ctx = context_block(project, files, note=note, allow_outside=_outside(args))
    user_text = first_message(ctx.text, args.question or "Review these pages as described in your instructions.")
    systems = {e.name: system_prompt(project, e, screenshots=True) for e in experts}
    if args.dry_run:
        return _dry_run(project, model, effort, args, list(systems.items()), user_text, images, ctx.sources,
                        note=f"(screenshots stay in {shots_dir})")
    site = args.site or base.split("//", 1)[-1].split("/")[0]
    out = report_path(project, site, args.label, today())
    keep = project.reviews_dir / "shots" / out.stem
    shutil.copytree(shots_dir, keep, dirs_exist_ok=True)
    shutil.rmtree(shots_dir, ignore_errors=True)
    images = [keep / i.name for i in images]
    head = header(f"Review {site} — {args.label} ({today()})",
                  _meta(project, model, effort, ctx.files, Pages=", ".join(args.pages),
                        Screenshots=f"{len(images)} in `{keep.relative_to(project.root) if keep.is_relative_to(project.root) else keep}`",
                        Experts=", ".join(e.name for e in experts)))
    _run_experts(project, experts, out, head,
                 lambda e: [{"role": "system", "content": systems[e.name]}, user_message(user_text, images)],
                 model, effort, args)


# ---------- personas, memory, init, status ----------

def cmd_personas(args):
    project = find_project()
    have = personas_mod.all_personas(project)
    if args.show:
        p = have.get(args.show) or personas_mod.resolve(project, [args.show])[0]
        print(f"{p.name}\n  title: {p.title}\n  instructions: {p.instructions or '-'}\n  source: {p.source}")
        return
    width = max(len(n) for n in have) if have else 10
    for name, p in sorted(have.items(), key=lambda kv: (kv[1].source != "global", kv[0])):
        origin = "global" if p.source == "global" else "project"
        print(f"{name:<{width}}  [{origin}]  {p.title}")
    if project.config_path:
        print(f"\npanel_personas: {', '.join(project.panel_personas) or '-'}\n"
              f"review_personas: {', '.join(project.review_personas) or '-'}")


def cmd_memory(args):
    project = find_project()
    f = project.memory_file
    if args.action == "show":
        print(f.read_text(encoding="utf-8") if f.is_file() else f"(empty: {f})")
        return
    if not args.text:
        raise ConsultError("memory add needs the text")
    f.parent.mkdir(parents=True, exist_ok=True)
    with f.open("a", encoding="utf-8") as fh:
        if not f.stat().st_size:
            fh.write("# Project memory for consultants\n\nOne line per fact or decision, newest last. "
                     "Sent with every consultation.\n\n")
        fh.write(f"- {today()}: {args.text.strip()}\n")
    print(f"added to {f}")


CONFIG_TEMPLATE = '''# consult – project configuration (see ~/consult/CLAUDE.md). Read by every `consult` command run inside this repo.
[project]
name = "{name}"
site = ""                       # https://… – base URL for `consult review` / `consult shots`
language = "en"                 # language of the answers (en, de, es, fr or any language name)
# What the project is and what it is trying to achieve – goes into every system prompt. Keep it to 3–6 sentences;
# details belong in the context files. State what is fixed (legal, privacy, budget) and what is open.
framing = """
"""
kpi = "the next measurable goal"   # what the synthesis ranks actions by, e.g. "confirmed sign-ups and the first euro"
context_files = []              # attached to every question, e.g. ["docs/goal.md", "docs/kpi.md"] (≤ 40k chars each)
reviews_dir = "{reviews}"       # where reports, threads and screenshots go
never_send = []                 # paths that must never reach a consultant, e.g. ["data/", "private/profiles/"]
default_role = ""               # persona for `consult ask` without --persona/--role
panel_personas = []             # defaults for `consult panel`, e.g. ["growth", "cfo", "skeptic"]
review_personas = []            # defaults for `consult review`, e.g. ["first-user", "conversion", "design"]

[model]
# name = "gpt-6-sol"            # override CONSULT_MODEL for this project (claude-… → Anthropic, vendor/model → OpenRouter)
# effort = "medium"

# Project-specific personas (override a global one by using its name). Reusable ones live in ~/consult/personas/.
# [personas.first-user]
# title = "…who they are, one sentence…"
# instructions = "…what to do with what they see…"
'''


def cmd_init(args):
    root = Path.cwd().resolve()
    cfg_dir = root / ("private/consult" if args.private else ".consult")
    cfg = cfg_dir / "config.toml"
    if cfg.exists():
        raise ConsultError(f"{cfg} already exists")
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "personas").mkdir(exist_ok=True)
    reviews = "private/reviews" if args.private else ".consult/reviews"
    cfg.write_text(CONFIG_TEMPLATE.format(name=root.name, reviews=reviews), encoding="utf-8")
    (root / reviews).mkdir(parents=True, exist_ok=True)
    print(f"wrote {cfg}\nnext: fill in framing/context_files, then paste {ROOT / 'docs' / 'project-snippet.md'} into "
          "the project's CLAUDE.md")
    _warn_git(root, [cfg_dir, root / reviews], private=args.private)


def _warn_git(root: Path, dirs: list[Path], private: bool):
    """A directory called private/ keeps nothing out of git. Say plainly what git will do with these paths."""
    if not (root / ".git").exists():
        return
    for d in dirs:
        rel = d.relative_to(root)
        tracked = _git_tracked(root, d)
        if tracked:
            print(f"WARNING: git already tracks {len(tracked)} file(s) under {rel}/ – they are (or will be) in the repo:")
            for t in tracked[:5]:
                print(f"  {t}")
        elif _git_ignored(root, d) is False:
            if private:
                print(f"WARNING: {rel}/ is NOT ignored by git – add it to .gitignore before you commit, or reports "
                      "with your goals and numbers end up in the repository")
            else:
                print(f"note: {rel}/ is not ignored – fine for a private repo; for a public one use "
                      "`consult init --private` or ignore it")


def cmd_status(args):
    project = find_project()
    try:
        model = resolve_model(project)
        provider = f"{describe(model)}; key {'ok' if api_key(model) else 'MISSING'}"
    except ConsultError as e:
        model, provider = f"error: {e}", "-"
    dangling = ROOT.joinpath(".env").is_symlink() and not ROOT.joinpath(".env").exists()
    print(f"consult root:   {ROOT}\n.env:           "
          f"{'dangling symlink – unlock your secret store' if dangling else 'ok' if ROOT.joinpath('.env').exists() else 'none (environment only)'}"
          f"\nprovider:       {provider}")
    web = "on (CONSULT_WEB)" if _web(argparse.Namespace()) else "off (--web per call, or CONSULT_WEB=on)"
    print(f"model:          {model}  (effort {resolve_effort(project)}; high effort/--best → "
          f"{setting('CONSULT_MODEL_BEST', 'OPENAI_MODEL_BEST') or 'not set (CONSULT_MODEL_BEST)'})\nweb search:     {web}")
    print(f"project:        {project.name}  root {project.root}\nconfig:         {project.config_path or '(none – defaults)'}")
    print(f"reviews dir:    {project.reviews_dir}\nmemory:         {project.memory_file} "
          f"({'exists' if project.memory_file.is_file() else 'empty'})")
    print(f"context files:  {', '.join(project.context_files) or '-'}")
    missing = [f for f in project.context_files if not (project.root / f).is_file()]
    if missing:
        print(f"  missing:      {', '.join(missing)}")
    print(f"personas:       {len(personas_mod.global_personas())} global, {len(project.personas)} project")
    print(f"cost cap:       ${_max_usd(args):.2f} per panel/review run (CONSULT_MAX_USD / --max-usd)")
    if project.config_path:
        for d in {project.config_dir, project.reviews_dir}:
            tracked = _git_tracked(project.root, d)
            if tracked:
                print(f"git:            {d.relative_to(project.root)}/ has {len(tracked)} tracked file(s) – "
                      "make sure this repo is private or these hold nothing confidential")
    node = shutil.which("node") or "(nvm)"
    pw = (ROOT / "node_modules" / "playwright").is_dir()
    print(f"screenshots:    node {node}, playwright {'installed' if pw else 'MISSING – npm install in ' + str(ROOT)}")


def cmd_snippet(args):
    print((ROOT / "docs" / "project-snippet.md").read_text(encoding="utf-8"))


def scratch_dir() -> Path:
    return Path(os.environ.get("TMPDIR", "/tmp"))


# ---------- prices, cost ----------

def cmd_prices(args):
    data = pricing.load(refresh=args.refresh)
    if not data:
        raise ConsultError("no price table: offline and no cache yet")
    age = (time.time() - data["fetched_at"]) / 86400
    print(f"USD per 1M tokens – OpenAI from {data['source']} ({age:.1f} days old; --refresh to fetch), Claude from "
          f"Anthropic's list prices as of {pricing.CLAUDE_PRICES_DATE}; OpenRouter reports each call's real cost. "
          f"Web search ${data['web_search_per_call'] * 1000:.2f} / 1k calls")
    project = find_project()
    names = args.models or sorted({resolve_model(project), setting("CONSULT_MODEL_BEST", "OPENAI_MODEL_BEST") or "",
                                   "gpt-5.4-mini"} - {""})
    print(f"  {'model':<30}{'input':>8}{'cached':>8}{'write':>8}{'output':>8}")
    for m in names:
        row = pricing.row_for(m, data)
        if row:
            print(f"  {m:<30}{row['input']:>8}{row.get('cached', '-'):>8}{row.get('cache_write', '-'):>8}{row['output']:>8}")
        else:
            print(f"  {m:<30} not on the page")


def cmd_cost(args):
    """Estimate a report's cost from its Model header and Tokens lines (older reports lack cost lines)."""
    import re
    data = pricing.load()
    if not data:
        raise ConsultError("no price table available")
    total = 0.0
    for f in args.files:
        text = Path(f).read_text(encoding="utf-8")
        m = re.search(r"Model: ([^;*]+)", text) or re.search(r"Modell: ([^,*]+)", text)
        model = m.group(1).strip() if m else "?"
        calls = [(int(a), int(b), int(w or 0)) for a, b, w in
                 re.findall(r"Tokens: (\d+) prompt / (\d+) completion(?:; web searches: (\d+))?", text)]
        if not calls:  # an older report format: "Tokens: 1234 / 567."
            calls = [(int(a), int(b), 0) for a, b in re.findall(r"Tokens: (\d+) / (\d+)", text)]
        usd = sum(pricing.cost(model, {"prompt_tokens": a, "completion_tokens": b, "web_searches": w}, data) or 0
                  for a, b, w in calls)
        total += usd
        print(f"{pricing.fmt(usd):>8}  {model:<14} {len(calls)} call(s)  {f}")
    if len(args.files) > 1:
        print(f"{pricing.fmt(total):>8}  total")
