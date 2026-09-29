"""Model access for three providers, all streamed, stdlib only (no SDKs):

- **openai** – OpenAI's Responses API, with the built-in web_search tool so consultants can search and open pages
  themselves; the older chat-completions endpoint as a fallback (`OPENAI_API=chat`).
- **anthropic** – Anthropic's Messages API (Claude), with the server-side web_search tool, adaptive thinking and
  `output_config.effort`; a paused server-tool turn (`pause_turn`) is resumed automatically.
- **openrouter** – OpenRouter's OpenAI-compatible chat completions (any model it routes, `vendor/model`), with its
  `web` plugin; OpenRouter reports the real cost of each call, which is used instead of an estimate.

The provider follows from the model name (`claude-…` → anthropic, `vendor/model` → openrouter, else openai) unless
`CONSULT_PROVIDER` says otherwise. Add another by implementing a function with `complete()`'s signature.

Streamed not because we show it live, but because a single long non-streaming request sits on a silent socket:
a 2026-09-18 run hung for the full 40-minute timeout with no way to tell thinking from a dead connection. Each
chunk resets the read timeout, so a real stall fails loudly in `stall` seconds instead of hiding.
"""

import base64
import http.client
import json
import mimetypes
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import pricing
from .config import ConsultError, Project, env, require_env, setting

PROVIDERS = {
    "openai": {"label": "OpenAI", "base": "https://api.openai.com/v1", "key": "OPENAI_API_KEY"},
    "anthropic": {"label": "Anthropic", "base": "https://api.anthropic.com/v1", "key": "ANTHROPIC_API_KEY"},
    "openrouter": {"label": "OpenRouter", "base": "https://openrouter.ai/api/v1", "key": "OPENROUTER_API_KEY"},
}
DEFAULT_MAX_TOKENS = 60_000  # reasoning tokens come out of the same budget: too small → an EMPTY answer after a wait
BEST_EFFORTS = ("high", "xhigh", "max")  # at these efforts the "best" model is used unless --model says otherwise
MAX_CONTINUATIONS = 5  # Anthropic: how often a paused server-tool turn is resumed before giving up
# Claude models that answer a refusal with a fallback model on the server (beta); see _anthropic()
CLAUDE_FALLBACK_MODELS = ("claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5")


def resolve_model(project: Project, override: str | None = None, effort: str = "", best: bool = False) -> str:
    if override:
        return override
    if best or effort in BEST_EFFORTS:
        top = setting("CONSULT_MODEL_BEST", "OPENAI_MODEL_BEST")
        if top:
            return top
    model = project.model or setting("CONSULT_MODEL", "OPENAI_MODEL", "OPENAI_REVIEW_MODEL")
    if not model:  # explicit on purpose: a silently picked "newest" model changes cost and behaviour unannounced
        raise ConsultError("no model configured – set CONSULT_MODEL (e.g. in .env, see .env.example), [model] name "
                           "in the project config, or pass --model")
    return model


def resolve_effort(project: Project, override: str | None = None) -> str:
    return override or project.effort or setting("CONSULT_EFFORT", "OPENAI_EFFORT", "OPENAI_CONSULT_EFFORT",
                                                 default="medium")


def provider_for(model: str) -> str:
    explicit = (env("CONSULT_PROVIDER") or "").lower()
    if explicit:
        if explicit not in PROVIDERS:
            raise ConsultError(f"CONSULT_PROVIDER={explicit!r} – use one of {', '.join(PROVIDERS)}")
        return explicit
    if "/" in model:
        return "openrouter"
    return "anthropic" if model.startswith("claude-") else "openai"


def describe(model: str) -> str:
    """One line for --dry-run and status: which provider, which endpoint, what it keeps."""
    name = provider_for(model)
    base = PROVIDERS[name]["base"]
    if name == "openai":
        api = "chat completions" if env("OPENAI_API", "responses") == "chat" else "Responses API"
        return f"OpenAI {api} ({base}), store=false"
    if name == "anthropic":
        return f"Anthropic Messages API ({base})"
    return f"OpenRouter chat completions ({base}); it forwards to the model's own provider"


def api_key(model: str) -> str | None:
    return env(PROVIDERS[provider_for(model)]["key"])


def is_reasoning_model(model: str) -> bool:
    return bool(re.match(r"(gpt-[5-9]|o\d)", model)) and "chat" not in model


def _claude_version(model: str) -> tuple[int, int]:
    """claude-opus-5-5 → (5, 5), claude-sonnet-4-6 → (4, 6), claude-haiku-4-5 → (4, 5); unknown → (0, 0)."""
    m = re.match(r"claude-[a-z]+-(\d+)(?:-(\d{1,2}))?(?:$|-)", model)
    return (int(m.group(1)), int(m.group(2) or 0)) if m else (0, 0)


def _claude_modern(model: str) -> bool:
    """Adaptive thinking, effort and the newer web search tool: every Claude 4.6+ model except Haiku."""
    return "haiku" not in model and _claude_version(model) >= (4, 6)


# ---------- messages (chat-completions shape; converted for the Responses and Messages APIs) ----------

def text_part(text: str) -> dict:
    return {"type": "text", "text": text}


def image_part(path: Path) -> list[dict]:
    """A screenshot as two parts: a caption with the file name, then the image."""
    if not path.is_file():
        return [text_part(f"[screenshot {path.name} no longer exists]")]
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    data = base64.b64encode(path.read_bytes()).decode()
    return [text_part(f"[Screenshot: {path.name}]"),
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}", "detail": "high"}}]


def user_message(text: str, images: list[Path] = ()) -> dict:
    if not images:
        return {"role": "user", "content": text}
    parts = [text_part(text)]
    for img in images:
        parts += image_part(Path(img))
    return {"role": "user", "content": parts}


def _to_responses_input(messages: list[dict]) -> tuple[str, list[dict]]:
    """system → instructions; user parts → input_text/input_image; assistant turns as plain text."""
    instructions, items = "", []
    for m in messages:
        if m["role"] == "system":
            instructions = m["content"]
        elif isinstance(m["content"], str):
            items.append({"role": m["role"], "content": m["content"]})
        else:
            parts = []
            for p in m["content"]:
                if p["type"] == "text":
                    parts.append({"type": "input_text", "text": p["text"]})
                else:
                    parts.append({"type": "input_image", "image_url": p["image_url"]["url"], "detail": "high"})
            items.append({"role": m["role"], "content": parts})
    return instructions, items


def _to_anthropic(messages: list[dict]) -> tuple[str, list[dict]]:
    """system → top-level system; data-URL images → base64 image blocks; assistant turns as plain text."""
    system, items = "", []
    for m in messages:
        if m["role"] == "system":
            system = m["content"]
        elif isinstance(m["content"], str):
            items.append({"role": m["role"], "content": m["content"]})
        else:
            parts = []
            for p in m["content"]:
                if p["type"] == "text":
                    parts.append({"type": "text", "text": p["text"]})
                else:
                    head, data = p["image_url"]["url"].split(",", 1)  # data:image/png;base64,…
                    parts.append({"type": "image", "source": {"type": "base64", "data": data,
                                                              "media_type": head[5:].split(";")[0]}})
            items.append({"role": m["role"], "content": parts})
    return system, items


# ---------- calls ----------

def complete(messages: list[dict], model: str, effort: str, max_tokens: int = DEFAULT_MAX_TOKENS,
             stall: int | None = None, quiet: bool = False, web: bool = True) -> tuple[str, dict, str | None]:
    """Run one completion. Returns (text, usage with prompt_tokens/completion_tokens/cost_usd, finish_reason)."""
    stall = stall or int(setting("CONSULT_STALL_SECONDS", "OPENAI_STALL_SECONDS", default="180"))
    name = provider_for(model)
    if name == "anthropic":
        return _anthropic(messages, model, effort, max_tokens, stall, quiet, web)
    if name == "openrouter":
        return _chat_completion(messages, model, effort, max_tokens, stall, quiet, web, provider="openrouter")
    if env("OPENAI_API", "responses") == "chat":
        return _chat_completion(messages, model, effort, max_tokens, stall, quiet, web=False)
    return _responses(messages, model, effort, max_tokens, stall, quiet, web)


def _headers(provider: str) -> dict:
    key = require_env(PROVIDERS[provider]["key"])
    if provider == "anthropic":
        return {"x-api-key": key, "anthropic-version": "2023-06-01"}
    headers = {"Authorization": "Bearer " + key}
    if provider == "openrouter":
        headers["X-Title"] = "consult"  # shows up in the OpenRouter activity log
    return headers


def _stream(provider: str, path: str, payload: dict, stall: int, extra_headers: dict | None = None):
    spec = PROVIDERS[provider]
    headers = {**_headers(provider), "Content-Type": "application/json", **(extra_headers or {})}
    req = urllib.request.Request(spec["base"] + path, data=json.dumps(payload).encode(), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=stall) as resp:
            for raw in resp:  # urllib's timeout applies per read → it is the stall detector
                line = raw.decode("utf-8").strip()
                if not line.startswith("data: "):  # SSE "event:" lines, comments and keep-alives
                    continue
                body = line[6:]
                if body == "[DONE]":
                    return
                try:
                    yield json.loads(body)
                except json.JSONDecodeError as e:
                    raise ConsultError(f"corrupt event in the model's stream: {body[:200]!r}") from e
    except urllib.error.HTTPError as e:
        raise ConsultError(f"{spec['label']} HTTP {e.code}: {e.read().decode(errors='replace')[:600]}") from e
    except TimeoutError as e:
        raise ConsultError(f"no data from the model for {stall}s – raise CONSULT_STALL_SECONDS or --stall") from e
    except (urllib.error.URLError, ConnectionError, http.client.HTTPException) as e:
        raise ConsultError(f"connection to {spec['base']} failed: {getattr(e, 'reason', e)}") from e


def _heartbeat(chunks: list[str], t0: float, last: float, quiet: bool) -> float:
    """A progress line every 15 s, so a long answer looks alive. Returns the time of the last line."""
    if not quiet and time.time() - last > 15:
        print(f"  … {sum(map(len, chunks))} chars after {time.time() - t0:.0f}s", file=sys.stderr)
        return time.time()
    return last


def _finish(text: str, usage: dict, finish: str | None, reasoning, max_tokens: int, t0: float, quiet: bool,
            extra: str = "", model: str = "") -> tuple[str, dict, str | None]:
    usage["model"] = model
    if usage.get("cost_usd") is None:  # OpenRouter reports the real cost; everyone else gets the list-price estimate
        usage["cost_usd"] = pricing.cost(model, usage)
    if not quiet:
        print(f"  done in {time.time() - t0:.0f}s: {len(text)} chars, finish={finish}, tokens "
              f"{usage.get('prompt_tokens')}/{usage.get('completion_tokens')} (reasoning={reasoning}){extra}, "
              f"cost ≈ {pricing.fmt(usage['cost_usd'])}", file=sys.stderr)
    if finish is None:  # the stream closed without a final event: never let a cut-off answer pass as finished
        raise ConsultError(f"the stream ended before the model finished ({len(text)} chars received, not saved)")
    if not text.strip():
        raise ConsultError(f"empty answer (finish={finish}, reasoning tokens={reasoning}): the budget went entirely "
                           f"into hidden reasoning – raise --max-tokens (now {max_tokens})")
    if finish in ("length", "max_output_tokens"):
        text += f"\n\n*[truncated: max output tokens={max_tokens} reached]*"
    elif finish != "stop":
        text += f"\n\n*[incomplete answer: the model stopped with finish={finish}]*"
    return text, usage, finish


def _responses(messages, model, effort, max_tokens, stall, quiet, web):
    instructions, items = _to_responses_input(messages)
    # store=False: the response is not kept for later retrieval (OpenAI's abuse-monitoring retention still applies)
    payload = {"model": model, "stream": True, "store": False, "max_output_tokens": max_tokens, "input": items}
    if instructions:
        payload["instructions"] = instructions
    if is_reasoning_model(model):
        payload["reasoning"] = {"effort": effort}
    if web:
        payload["tools"] = [{"type": "web_search"}]
    chunks, usage, finish, reasoning, searches = [], {}, None, None, 0
    t0 = last = time.time()
    for ev in _stream("openai", "/responses", payload, stall):
        kind = ev.get("type", "")
        if kind == "response.output_text.delta":
            chunks.append(ev.get("delta", ""))
            last = _heartbeat(chunks, t0, last, quiet)
        elif kind == "response.web_search_call.searching":
            searches += 1
            if not quiet:
                print(f"  … web search #{searches} after {time.time() - t0:.0f}s", file=sys.stderr)
        elif kind in ("response.completed", "response.incomplete", "response.failed"):
            r = ev.get("response", {})
            u = r.get("usage") or {}
            d = u.get("input_tokens_details") or {}
            usage = {"prompt_tokens": u.get("input_tokens"), "completion_tokens": u.get("output_tokens"),
                     "cached_tokens": d.get("cached_tokens"), "cache_write_tokens": d.get("cache_write_tokens"),
                     "web_searches": searches}
            reasoning = (u.get("output_tokens_details") or {}).get("reasoning_tokens")
            finish = (r.get("incomplete_details") or {}).get("reason") or ("stop" if kind == "response.completed" else kind)
            if kind == "response.failed":
                raise ConsultError(f"model call failed: {json.dumps(r.get('error'))[:400]}")
        elif kind == "error":
            raise ConsultError(f"stream error: {json.dumps(ev)[:400]}")
    return _finish("".join(chunks), usage, finish, reasoning, max_tokens, t0, quiet,
                   extra=f", web searches={searches}" if web else "", model=model)


def _chat_completion(messages, model, effort, max_tokens, stall, quiet, web, provider="openai"):
    payload = {"model": model, "max_completion_tokens": max_tokens, "stream": True, "messages": messages}
    if provider == "openrouter":
        payload["reasoning"] = {"effort": effort}  # OpenRouter maps it per model; ignored where there is none
        if web:
            payload["plugins"] = [{"id": "web"}]
    else:
        payload["stream_options"] = {"include_usage": True}
        if is_reasoning_model(model):
            payload["reasoning_effort"] = effort
    chunks, usage, finish, t0, last = [], {}, None, time.time(), time.time()
    for ev in _stream(provider, "/chat/completions", payload, stall):
        if ev.get("error"):  # OpenRouter reports mid-stream failures as an event with an error object
            raise ConsultError(f"model call failed: {json.dumps(ev['error'])[:400]}")
        if ev.get("usage"):
            usage = ev["usage"]
        for ch in ev.get("choices") or []:
            finish = ch.get("finish_reason") or finish
            piece = (ch.get("delta") or {}).get("content")
            if piece:
                chunks.append(piece)
                last = _heartbeat(chunks, t0, last, quiet)
    reasoning = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
    cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
    usage = {"prompt_tokens": usage.get("prompt_tokens"), "completion_tokens": usage.get("completion_tokens"),
             "cached_tokens": cached, "cost_usd": usage.get("cost"), "web_searches": 0}
    return _finish("".join(chunks), usage, finish, reasoning, max_tokens, t0, quiet,
                   extra=", web plugin on" if web and provider == "openrouter" else "", model=model)


def _anthropic(messages, model, effort, max_tokens, stall, quiet, web):
    """Anthropic's Messages API. The full content blocks are rebuilt from the stream, because a server-tool loop
    that hits its iteration limit stops with `pause_turn` and is resumed by sending the turn back unchanged."""
    system, items = _to_anthropic(messages)
    payload = {"model": model, "max_tokens": max_tokens, "stream": True, "messages": items}
    if system:
        payload["system"] = system
    if _claude_modern(model):
        payload["thinking"] = {"type": "adaptive"}
        payload["output_config"] = {"effort": effort}
    if web:
        payload["tools"] = [{"type": "web_search_20260209" if _claude_modern(model) else "web_search_20250305",
                             "name": "web_search"}]
    headers = {}
    if model in CLAUDE_FALLBACK_MODELS:  # a safety-classifier refusal is re-run on a fallback model in the same call
        payload["fallbacks"] = "default"
        headers["anthropic-beta"] = "server-side-fallback-2026-07-01"
    total = {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0, "cache_write_tokens": 0,
             "web_searches": 0}
    chunks, finish, served, notes = [], None, model, []
    t0 = last = time.time()
    for _ in range(MAX_CONTINUATIONS + 1):
        blocks, stop, details, usage = [], None, None, {}
        for ev in _stream("anthropic", "/messages", payload, stall, headers):
            kind = ev.get("type", "")
            if kind == "message_start":
                msg = ev.get("message") or {}
                served = msg.get("model") or served
                usage.update(msg.get("usage") or {})
            elif kind == "content_block_start":
                block = dict(ev.get("content_block") or {})
                if block.get("type") == "server_tool_use":
                    block["_json"] = ""
                    total["web_searches"] += 1
                    if not quiet:
                        print(f"  … web search #{total['web_searches']} after {time.time() - t0:.0f}s",
                              file=sys.stderr)
                elif block.get("type") == "fallback":
                    notes.append(f"{(block.get('from') or {}).get('model')} declined, "
                                 f"{(block.get('to') or {}).get('model')} continued")
                blocks.append(block)
            elif kind == "content_block_delta" and blocks:
                block, d = blocks[-1], ev.get("delta") or {}
                if d.get("type") == "text_delta":
                    block["text"] = block.get("text", "") + d.get("text", "")
                    chunks.append(d.get("text", ""))
                    last = _heartbeat(chunks, t0, last, quiet)
                elif d.get("type") == "thinking_delta":
                    block["thinking"] = block.get("thinking", "") + d.get("thinking", "")
                elif d.get("type") == "signature_delta":
                    block["signature"] = block.get("signature", "") + d.get("signature", "")
                elif d.get("type") == "input_json_delta":
                    block["_json"] = block.get("_json", "") + d.get("partial_json", "")
                elif d.get("type") == "citations_delta":
                    block["citations"] = (block.get("citations") or []) + [d.get("citation")]
            elif kind == "content_block_stop" and blocks and "_json" in blocks[-1]:
                raw = blocks[-1].pop("_json")
                blocks[-1]["input"] = json.loads(raw) if raw else blocks[-1].get("input", {})
            elif kind == "message_delta":
                stop = (ev.get("delta") or {}).get("stop_reason") or stop
                details = (ev.get("delta") or {}).get("stop_details") or details
                usage.update(ev.get("usage") or {})
            elif kind == "error":
                raise ConsultError(f"stream error: {json.dumps(ev.get('error') or ev)[:400]}")
        cached, written = usage.get("cache_read_input_tokens") or 0, usage.get("cache_creation_input_tokens") or 0
        total["prompt_tokens"] += (usage.get("input_tokens") or 0) + cached + written  # input_tokens excludes both
        total["completion_tokens"] += usage.get("output_tokens") or 0  # includes thinking
        total["cached_tokens"] += cached
        total["cache_write_tokens"] += written
        if stop == "refusal":  # before or during output; a partial answer from a refusal is discarded, not saved
            raise ConsultError(f"the model declined (stop_reason=refusal): {json.dumps(details)[:300]}")
        if stop != "pause_turn":
            break
        if not quiet:
            print(f"  … resuming a paused turn after {time.time() - t0:.0f}s", file=sys.stderr)
        # resume: send the paused turn back as it is; the server continues where it stopped
        payload["messages"] = payload["messages"] + [{"role": "assistant", "content": blocks}]
    finish = {"end_turn": "stop", "stop_sequence": "stop", "max_tokens": "length"}.get(stop, stop)
    if served != model:
        notes.append(f"served by {served}")
    extra = (f", web searches={total['web_searches']}" if web else "") + "".join(f"; {n}" for n in notes)
    return _finish("".join(chunks), total, finish, None, max_tokens, t0, quiet, extra=extra, model=served)
