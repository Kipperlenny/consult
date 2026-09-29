"""What a consultation costs. OpenAI prices come from its public pricing page (platform.openai.com/docs/pricing),
whose HTML embeds the standard table as data; they are cached for a week in .cache/prices.json. Claude prices are a
small table below (Anthropic's list prices, dated). OpenRouter reports the real cost of every call, so it needs no
table; for its dry-run estimates `vendor/model` is looked up as `model`. The providers' usage APIs would be exact
but need admin keys, so this estimates from the tokens each call reports:
input (minus cached, minus cache writes) × input price + cached × cached price + cache writes × write price
+ output (incl. hidden reasoning) × output price + web searches × per-call price. USD, per 1M tokens.
"""

import html
import json
import re
import time
import urllib.request
from pathlib import Path

from .config import ROOT

PRICING_URL = "https://platform.openai.com/docs/pricing"
CACHE = ROOT / ".cache" / "prices.json"
MAX_AGE = 7 * 86400
WEB_SEARCH_PER_CALL = 0.01  # "$10.00 / 1k calls" on the same page; parsed too when found; the same at Anthropic

# Anthropic list prices, USD per 1M tokens (platform.claude.com/docs/en/about-claude/pricing), as of 2026-09-25.
# Cache writes (5-minute TTL) cost 1.25 × input. Update by hand when they change – `consult prices` shows the date.
CLAUDE_PRICES_DATE = "2026-09-25"
CLAUDE_PRICES = {
    "claude-fable-5-1": {"input": 10.0, "cached": 0.25, "output": 50.0},
    "claude-fable-5": {"input": 10.0, "cached": 1.0, "output": 50.0},
    "claude-opus-5-5": {"input": 4.0, "cached": 0.2, "output": 20.0},
    "claude-opus-5": {"input": 5.0, "cached": 0.5, "output": 25.0},
    "claude-opus-4-8": {"input": 5.0, "cached": 0.5, "output": 25.0},
    "claude-opus-4-7": {"input": 5.0, "cached": 0.5, "output": 25.0},
    "claude-opus-4-6": {"input": 5.0, "cached": 0.5, "output": 25.0},
    "claude-sonnet-5-5": {"input": 2.0, "cached": 0.2, "output": 10.0},
    "claude-sonnet-5": {"input": 2.0, "cached": 0.2, "output": 10.0},
    "claude-sonnet-4-6": {"input": 3.0, "cached": 0.3, "output": 15.0},
    "claude-haiku-4-5": {"input": 1.0, "cached": 0.1, "output": 5.0},
}
for _row in CLAUDE_PRICES.values():
    _row["cache_write"] = _row["input"] * 1.25


def _parse(page: str) -> dict:
    """{model: {input, cached, cache_write?, output}} from the first (standard) table that lists gpt models."""
    text = html.unescape(page)
    start = text.find('"rows":[1,[[1,[[0,"gpt-')
    if start < 0:
        raise ValueError("pricing table not found on the page – its format changed")
    end = text.find(']]]]', start)  # end of the rows array
    prices = {}
    for name, cells in re.findall(r'\[1,\[\[0,"([^"]+)"\]((?:,\[0,[^\]]*\])+)\]\]', text[start:end + 4]):
        vals = [v.strip('"') for v in re.findall(r'\[0,([^\]]*)\]', cells)]
        nums = [float(v) if re.fullmatch(r"\d+(\.\d+)?", v) else None for v in vals]
        key = name.split(" ")[0]  # "gpt-5.5 (<272K context length)" → gpt-5.5
        if len(nums) == 4:
            row = {"input": nums[0], "cached": nums[1], "cache_write": nums[2], "output": nums[3]}
        elif len(nums) == 3:
            row = {"input": nums[0], "cached": nums[1], "output": nums[2]}
        else:
            continue
        prices.setdefault(key, row)
    m = re.search(r"Web search \(all models\)[^$]*\$(\d+(?:\.\d+)?) / 1k calls", text)
    web = float(m.group(1)) / 1000 if m else WEB_SEARCH_PER_CALL
    return {"fetched_at": int(time.time()), "source": PRICING_URL, "web_search_per_call": web, "models": prices}


def load(refresh: bool = False) -> dict | None:
    """The cached price table, refreshed from the page when older than a week. None when unavailable offline."""
    if not refresh and CACHE.is_file():
        data = json.loads(CACHE.read_text(encoding="utf-8"))
        if time.time() - data.get("fetched_at", 0) < MAX_AGE:
            return data
    try:
        req = urllib.request.Request(PRICING_URL, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) consult"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = _parse(r.read().decode("utf-8", errors="replace"))
    except Exception:  # noqa: BLE001 – offline or format change: fall back to the stale cache, or nothing
        return json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.is_file() else None
    CACHE.parent.mkdir(exist_ok=True)
    CACHE.write_text(json.dumps(data, indent=1), encoding="utf-8")
    return data


def row_for(model: str, data: dict | None) -> dict | None:
    model = model.split("/")[-1].split(":")[0]  # OpenRouter: openai/gpt-6-sol:online → gpt-6-sol
    if model.startswith("claude-"):
        return CLAUDE_PRICES.get(model) or CLAUDE_PRICES.get(model.replace(".", "-"))  # anthropic/claude-opus-5.5
    if not data:
        return None
    models = data["models"]
    base = re.sub(r"-\d{4}-\d{2}-\d{2}$", "", model)  # gpt-5.4-2026-03-05 → gpt-5.4
    for key in (model, base, base.replace("-chat-latest", "")):
        if key in models:
            return models[key]
    return None


def cost(model: str, usage: dict, data: dict | None = None) -> float | None:
    if not model.split("/")[-1].startswith("claude-"):
        data = data or load()
    row = row_for(model, data)
    if not row:
        return None
    data = data or {}
    inp = usage.get("prompt_tokens") or 0
    cached = usage.get("cached_tokens") or 0
    written = usage.get("cache_write_tokens") or 0
    out = usage.get("completion_tokens") or 0
    plain = max(0, inp - cached - written)
    usd = (plain * row["input"] + cached * (row.get("cached") or row["input"])
           + written * (row.get("cache_write") or row["input"]) + out * row["output"]) / 1e6
    usd += (usage.get("web_searches") or 0) * data.get("web_search_per_call", WEB_SEARCH_PER_CALL)
    return usd


def fmt(usd: float | None) -> str:
    return "n/a" if usd is None else (f"${usd:.4f}" if usd < 0.01 else f"${usd:.2f}")
