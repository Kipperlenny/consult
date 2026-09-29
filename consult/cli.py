"""Command line: `consult <command> …`. Run `consult --help` or `consult <command> --help`."""

import argparse
import sys

from . import __version__, commands
from .config import ConsultError
from .providers import DEFAULT_MAX_TOKENS


def _model_args(p):
    p.add_argument("--model", help="override the model (else project config [model] name, else CONSULT_MODEL); claude-… uses Anthropic, "
                   "vendor/model OpenRouter, anything else OpenAI (or set CONSULT_PROVIDER)")
    p.add_argument("--effort", help="reasoning effort: low, medium, high, xhigh (else project config / CONSULT_EFFORT)")
    p.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS,
                   help="completion budget incl. hidden reasoning; too small returns an empty answer, not a short one")
    p.add_argument("--stall", type=int, help="fail if the stream is silent this many seconds (CONSULT_STALL_SECONDS)")
    p.add_argument("--best", action="store_true", help="use CONSULT_MODEL_BEST (automatic at --effort high/xhigh)")
    p.add_argument("--web", action="store_true", help="let the model search the web and open pages itself (off unless "
                   "CONSULT_WEB=on); results it reads go to the provider like everything else")
    p.add_argument("--no-web", action="store_true", help="no web_search tool, even with CONSULT_WEB=on (the question "
                   "and context still go to the model provider)")
    p.add_argument("--max-usd", type=float, help="stop a panel/review when the estimated spend passes this "
                   "(default CONSULT_MAX_USD or 5)")
    p.add_argument("--dry-run", action="store_true", help="print exactly what would be sent (prompts, files with "
                   "their resolved targets, memory, thread, images, pages, model, estimated tokens) – no model call")


def _context_args(p):
    p.add_argument("--files", nargs="*", help="context files (relative to the project root); replaces the "
                   "project's context_files list. Files matching never_send are refused")
    p.add_argument("--images", nargs="*", default=[], help="PNG/JPEG screenshots to attach")
    p.add_argument("--pages", nargs="*", help="pages to attach as text + screenshots: paths on project.site or full URLs")
    p.add_argument("--base", help="base URL for --pages (default project.site)")
    p.add_argument("--allow-outside", action="store_true", help="allow files/images outside the project root")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="consult", description=__doc__.split("\n")[0])
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("ask", help="one question to one expert (persona or invented role); saves a report")
    p.add_argument("--label", required=True, help="short name for the report file")
    p.add_argument("--question", "-q", required=True)
    p.add_argument("--persona", help="a persona name (see `consult personas`)")
    p.add_argument("--role", help="invent the expert instead: free text, e.g. 'pricing strategist for SaaS in DACH'")
    p.add_argument("--system", help="replace the whole system prompt (prompt experiments)")
    p.add_argument("--dialog", action="store_true", help="invite follow-up questions and screenshot requests")
    p.add_argument("--thread", help="continue as a thread instead (see `consult chat`)")
    _context_args(p)
    _model_args(p)
    p.set_defaults(func=commands.cmd_ask)

    p = sub.add_parser("chat", help="a multi-round conversation with memory: the thread keeps every round")
    p.add_argument("--thread", required=True, help="thread name (stored in <reviews_dir>/threads/) or a .json path")
    p.add_argument("--question", "-q")
    p.add_argument("--label", help="report name (first round only; default: the thread name)")
    p.add_argument("--persona")
    p.add_argument("--role")
    p.add_argument("--system")
    p.add_argument("--show", action="store_true", help="print the thread so far instead of asking")
    _context_args(p)
    _model_args(p)
    p.set_defaults(func=commands.cmd_chat)

    p = sub.add_parser("panel", help="several experts answer the same question blind, then one synthesis")
    p.add_argument("--label", required=True)
    p.add_argument("--question", "-q", required=True)
    p.add_argument("--personas", help="comma-separated persona names (default: the project's panel_personas)")
    p.add_argument("--roles", nargs="*", help="additional invented experts, free text each")
    p.add_argument("--gonogo", help="a yes/no decision the synthesis must answer with GO or NO GO")
    _context_args(p)
    _model_args(p)
    p.set_defaults(func=commands.cmd_panel)

    p = sub.add_parser("review", help="screenshots of pages → personas → synthesis with ranked actions")
    p.add_argument("--pages", nargs="+", required=True, help="paths on the site, e.g. / /pricing")
    p.add_argument("--label", default="review")
    p.add_argument("--site", help="host name (default: from project.site)")
    p.add_argument("--base", help="base URL (default: https://<site> or project.site)")
    p.add_argument("--images", nargs="*", default=[], help=argparse.SUPPRESS)
    p.add_argument("--personas", help="comma-separated (default: the project's review_personas)")
    p.add_argument("--question", "-q", help="what to look at specifically (default: each persona's instructions)")
    p.add_argument("--gonogo", help="decision question for the synthesis")
    p.add_argument("--files", nargs="*", help="context files (replaces the project's context_files)")
    p.add_argument("--allow-outside", action="store_true", help="allow files outside the project root")
    _model_args(p)
    p.set_defaults(func=commands.cmd_review)

    p = sub.add_parser("shots", help="only take desktop + mobile screenshots of pages (no model call)")
    p.add_argument("--pages", nargs="+", required=True)
    p.add_argument("--site")
    p.add_argument("--base")
    p.add_argument("--out", help="directory (default: <reviews_dir>/shots/<timestamp>)")
    p.set_defaults(func=commands.cmd_shots)

    p = sub.add_parser("personas", help="list personas (global + project)")
    p.add_argument("--show", help="print one persona in full")
    p.set_defaults(func=commands.cmd_personas)

    p = sub.add_parser("memory", help="project memory sent with every consultation")
    p.add_argument("action", choices=["add", "show"])
    p.add_argument("text", nargs="?", help="the fact or decision to remember (for add)")
    p.set_defaults(func=commands.cmd_memory)

    p = sub.add_parser("init", help="create .consult/config.toml in the current project")
    p.add_argument("--private", action="store_true", help="use private/consult/ instead (for public repositories)")
    p.set_defaults(func=commands.cmd_init)

    p = sub.add_parser("status", help="resolved configuration, key, model, tools")
    p.set_defaults(func=commands.cmd_status)

    p = sub.add_parser("prices", help="current list prices (OpenAI's pricing page, cached a week; Claude table)")
    p.add_argument("models", nargs="*", help="models to show (default: the configured ones)")
    p.add_argument("--refresh", action="store_true")
    p.set_defaults(func=commands.cmd_prices)

    p = sub.add_parser("cost", help="estimate what a report cost from its token lines")
    p.add_argument("files", nargs="+", help="report .md files")
    p.set_defaults(func=commands.cmd_cost)

    p = sub.add_parser("snippet", help="print the CLAUDE.md snippet to paste into a project")
    p.set_defaults(func=commands.cmd_snippet)

    args = ap.parse_args(argv)
    try:
        args.func(args)
    except ConsultError as e:
        sys.exit(f"consult: {e}")
    except KeyboardInterrupt:
        sys.exit(130)
