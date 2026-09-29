"""Markdown reports in the project's reviews directory. Every run leaves one file with the exact inputs (model,
effort, files, system prompt, question) and the outputs, so a later reader can judge what the advice rested on."""

import re
from pathlib import Path

from .config import Project
from .pricing import fmt


def slug(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-") or "untitled"


def report_path(project: Project, kind: str, label: str, date: str) -> Path:
    project.reviews_dir.mkdir(parents=True, exist_ok=True)
    return project.reviews_dir / f"{date}-{kind}-{slug(label)}.md"


def header(title: str, meta: dict) -> str:
    items = "; ".join(f"{k}: {v}" for k, v in meta.items() if v not in (None, "", []))
    return f"# {title}\n\n*{items}.*\n\n"


def quote(text: str) -> str:
    return "> " + text.strip().replace("\n", "\n> ")


def tokens_line(usage: dict) -> str:
    cost = usage.get("cost_usd")
    return (f"*Tokens: {usage.get('prompt_tokens')} prompt / {usage.get('completion_tokens')} completion"
            + (f"; web searches: {usage['web_searches']}" if usage.get("web_searches") else "")
            + (f"; cost ≈ {fmt(cost)} (list prices, estimate)" if cost is not None else "") + ".*")


def sum_usage(usages: list[dict]) -> dict:
    costs = [u.get("cost_usd") for u in usages]
    return {"prompt_tokens": sum(u.get("prompt_tokens") or 0 for u in usages),
            "completion_tokens": sum(u.get("completion_tokens") or 0 for u in usages),
            "web_searches": sum(u.get("web_searches") or 0 for u in usages),
            "cost_usd": sum(c for c in costs if c is not None) if any(c is not None for c in costs) else None}
