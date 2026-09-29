"""Offline tests: no API key, no network, no browser. `python3 -m unittest discover -s tests` from the repo root.

The real toolkit .env is replaced by a missing file, urllib is replaced by fakes that replay a stream, and prices come
from a saved excerpt of the pricing page. The point is the failure paths: a failed or partial run must never look
like a finished one, and nothing on the never_send list or outside the project leaves unnoticed.
"""

import contextlib
import email.message
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from consult import cli, commands, config, pricing, providers  # noqa: E402
from consult.config import ConsultError  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
PRICES = pricing._parse((FIXTURES / "pricing-excerpt.html").read_text(encoding="utf-8"))
FAKE_KEY = "sk-test-DO-NOT-LEAK-1234"


def sse(*events) -> list[bytes]:
    return [f"data: {json.dumps(e)}\n".encode() for e in events] + [b"\n"]


def delta(text):
    return {"type": "response.output_text.delta", "delta": text}


def completed(kind="response.completed", reason=None, inp=1000, out=500):
    r = {"usage": {"input_tokens": inp, "output_tokens": out, "output_tokens_details": {"reasoning_tokens": 100}}}
    if reason:
        r["incomplete_details"] = {"reason": reason}
    return {"type": kind, "response": r}


class FakeResponse:
    """What urlopen returns: a context manager that yields the stream's lines, then optionally raises."""

    def __init__(self, lines, then=None):
        self.lines, self.then = lines, then

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        yield from self.lines
        if self.then:
            raise self.then


class Base(unittest.TestCase):
    """A throw-away project, no real .env, fixed prices, a fake key in the environment."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="consult-test-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        (self.tmp / ".consult").mkdir()
        (self.tmp / "docs").mkdir()
        (self.tmp / "data").mkdir()
        (self.tmp / "docs" / "goal.md").write_text("Goal: ten users.\n")
        (self.tmp / "data" / "subscribers.csv").write_text("a@example.org\n")
        (self.tmp / ".consult" / "config.toml").write_text(
            '[project]\nname = "Test"\nsite = "https://example.org"\nlanguage = "en"\nkpi = "users"\n'
            'context_files = ["docs/goal.md"]\nnever_send = ["data/", "plan.md"]\npanel_personas = ["growth", "cfo"]\n'
            'review_personas = ["growth"]\n')
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("OPENAI_", "CONSULT_", "ANTHROPIC_", "OPENROUTER_"))}
        env.update(OPENAI_API_KEY=FAKE_KEY, OPENAI_MODEL="gpt-6-sol",
                   CONSULT_CONFIG=str(self.tmp / ".consult" / "config.toml"))
        for p in (mock.patch.dict(os.environ, env, clear=True),
                  mock.patch.object(config, "ENV_FILE", self.tmp / "no-such.env"),
                  mock.patch.object(pricing, "load", lambda refresh=False: PRICES)):
            p.start()
            self.addCleanup(p.stop)
        self.stderr = io.StringIO()
        p = mock.patch.object(sys, "stderr", self.stderr)
        p.start()
        self.addCleanup(p.stop)

    def run_cli(self, *argv) -> str:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(list(argv))
        return out.getvalue()

    def reports(self, pattern="*.md") -> list[Path]:
        return sorted((self.tmp / ".consult" / "reviews").glob(pattern))


# ---------- the provider: every way a call can go wrong ----------

class ProviderTests(Base):
    MSGS = [{"role": "system", "content": "s"}, {"role": "user", "content": "q"}]

    def call(self, response=None, **kw):
        fake = mock.Mock(side_effect=response) if isinstance(response, Exception) else mock.Mock(return_value=response)
        with mock.patch("urllib.request.urlopen", fake):
            return providers.complete(self.MSGS, "gpt-6-sol", "medium", **kw), fake

    def test_missing_key(self):
        del os.environ["OPENAI_API_KEY"]
        with self.assertRaisesRegex(ConsultError, "OPENAI_API_KEY"):
            self.call(FakeResponse([]))

    def test_missing_model_is_an_error_not_a_guess(self):
        del os.environ["OPENAI_MODEL"]
        with self.assertRaisesRegex(ConsultError, "no model configured"):
            providers.resolve_model(config.find_project())

    def test_http_error(self):
        err = urllib.error.HTTPError("u", 401, "Unauthorized", email.message.Message(),
                                     io.BytesIO(b'{"error": {"message": "Incorrect API key"}}'))
        with self.assertRaisesRegex(ConsultError, "OpenAI HTTP 401.*Incorrect API key"):
            self.call(err)

    def test_connection_refused(self):
        with self.assertRaisesRegex(ConsultError, "connection to .* failed"):
            self.call(urllib.error.URLError(ConnectionRefusedError("refused")))

    def test_corrupt_stream_event(self):
        with self.assertRaisesRegex(ConsultError, "corrupt event"):
            self.call(FakeResponse(sse(delta("Hel")) + [b"data: {not json\n"]))

    def test_stall(self):
        with self.assertRaisesRegex(ConsultError, "no data from the model for 7s"):
            self.call(FakeResponse(sse(delta("Hello")), then=TimeoutError()), stall=7)

    def test_stream_cut_off_is_not_an_answer(self):
        with self.assertRaisesRegex(ConsultError, "stream ended before the model finished"):
            self.call(FakeResponse(sse(delta("A long answer that just stops"))))

    def test_failed_response(self):
        with self.assertRaisesRegex(ConsultError, "model call failed"):
            self.call(FakeResponse(sse({"type": "response.failed", "response": {"error": {"code": "server_error"}}})))

    def test_empty_answer(self):
        with self.assertRaisesRegex(ConsultError, "empty answer"):
            self.call(FakeResponse(sse(completed())))

    def test_truncated_and_incomplete_are_marked(self):
        (text, _, finish), _ = self.call(FakeResponse(sse(delta("Part"), completed("response.incomplete",
                                                                                   "max_output_tokens"))))
        self.assertIn("[truncated", text)
        (text, _, finish), _ = self.call(FakeResponse(sse(delta("Part"), completed("response.incomplete",
                                                                                   "content_filter"))))
        self.assertIn("[incomplete answer", text)
        self.assertEqual(finish, "content_filter")

    def test_success_usage_cost_and_payload(self):
        (text, usage, finish), fake = self.call(FakeResponse(sse(delta("Hello "), delta("world"), completed())))
        self.assertEqual((text, finish), ("Hello world", "stop"))
        self.assertAlmostEqual(usage["cost_usd"], (1000 * 2 + 500 * 10) / 1e6)  # gpt-6-sol from the fixture
        payload = json.loads(fake.call_args.args[0].data)
        self.assertIs(payload["store"], False)
        self.assertEqual(payload["tools"], [{"type": "web_search"}])  # complete() itself defaults to web=True …


def claude_events(*texts, stop="end_turn", inp=1000, out=500, model="claude-opus-5-5", searches=0):
    """A Messages API stream: optional web searches, one text block, the final stop reason and usage."""
    evs = [{"type": "message_start", "message": {"model": model, "usage": {"input_tokens": inp,
                                                                           "cache_read_input_tokens": 0}}}]
    for i in range(searches):
        evs += [{"type": "content_block_start", "index": len(evs),
                 "content_block": {"type": "server_tool_use", "id": f"s{i}", "name": "web_search", "input": {}}},
                {"type": "content_block_delta", "delta": {"type": "input_json_delta", "partial_json": '{"query": "x"}'}},
                {"type": "content_block_stop"}]
    if texts:
        evs.append({"type": "content_block_start", "content_block": {"type": "text", "text": ""}})
        evs += [{"type": "content_block_delta", "delta": {"type": "text_delta", "text": t}} for t in texts]
        evs.append({"type": "content_block_stop"})
    evs += [{"type": "message_delta", "delta": {"stop_reason": stop}, "usage": {"output_tokens": out}},
            {"type": "message_stop"}]
    return sse(*evs)


class OtherProviderTests(Base):
    MSGS = [{"role": "system", "content": "sys"}, {"role": "user", "content": "q"}]

    def setUp(self):
        super().setUp()
        os.environ.update(ANTHROPIC_API_KEY=FAKE_KEY, OPENROUTER_API_KEY=FAKE_KEY)

    def call(self, model, *responses, **kw):
        fake = mock.Mock(side_effect=list(responses))
        with mock.patch("urllib.request.urlopen", fake):
            return providers.complete(self.MSGS, model, "high", **kw), fake

    def test_provider_follows_the_model_name_unless_set(self):
        self.assertEqual(providers.provider_for("gpt-6-sol"), "openai")
        self.assertEqual(providers.provider_for("claude-opus-5-5"), "anthropic")
        self.assertEqual(providers.provider_for("anthropic/claude-opus-5-5"), "openrouter")
        os.environ["CONSULT_PROVIDER"] = "openrouter"
        self.assertEqual(providers.provider_for("claude-opus-5-5"), "openrouter")
        os.environ["CONSULT_PROVIDER"] = "bogus"
        with self.assertRaisesRegex(ConsultError, "CONSULT_PROVIDER"):
            providers.provider_for("gpt-6-sol")

    def test_generic_names_win_over_old_openai_names(self):
        os.environ["CONSULT_MODEL"] = "claude-sonnet-5-5"
        self.assertEqual(providers.resolve_model(config.find_project()), "claude-sonnet-5-5")

    def test_missing_anthropic_key(self):
        del os.environ["ANTHROPIC_API_KEY"]
        with self.assertRaisesRegex(ConsultError, "ANTHROPIC_API_KEY"):
            self.call("claude-opus-5-5", FakeResponse([]))

    def test_claude_success_payload_and_cost(self):
        (text, usage, finish), fake = self.call("claude-opus-5-5", FakeResponse(claude_events("Hello ", "world")))
        self.assertEqual((text, finish), ("Hello world", "stop"))
        self.assertAlmostEqual(usage["cost_usd"], (1000 * 4 + 500 * 20) / 1e6)  # the Claude table, no page needed
        req = fake.call_args.args[0]
        payload = json.loads(req.data)
        self.assertEqual(payload["system"], "sys")
        self.assertEqual(payload["messages"], [{"role": "user", "content": "q"}])
        self.assertEqual(payload["thinking"], {"type": "adaptive"})
        self.assertEqual(payload["output_config"], {"effort": "high"})
        self.assertEqual(payload["tools"], [{"type": "web_search_20260209", "name": "web_search"}])
        self.assertEqual(payload["fallbacks"], "default")
        self.assertEqual(req.get_header("X-api-key"), FAKE_KEY)
        self.assertIsNone(req.get_header("Authorization"))

    def test_old_claude_gets_no_effort_and_the_basic_search_tool(self):
        _, fake = self.call("claude-haiku-4-5", FakeResponse(claude_events("ok")))
        payload = json.loads(fake.call_args.args[0].data)
        self.assertNotIn("output_config", payload)
        self.assertNotIn("fallbacks", payload)
        self.assertEqual(payload["tools"][0]["type"], "web_search_20250305")

    def test_claude_images_become_base64_blocks(self):
        img = self.tmp / "shot.png"
        img.write_bytes(b"\x89PNG fake")
        system, items = providers._to_anthropic([providers.user_message("look", [img])])
        block = items[0]["content"][2]
        self.assertEqual(block["type"], "image")
        self.assertEqual(block["source"]["media_type"], "image/png")

    def test_claude_paused_turn_is_resumed(self):
        (text, usage, finish), fake = self.call(
            "claude-opus-5-5", FakeResponse(claude_events("First ", stop="pause_turn", searches=1)),
            FakeResponse(claude_events("second.", searches=1)))
        self.assertEqual((text, finish), ("First second.", "stop"))
        self.assertEqual(fake.call_count, 2)
        self.assertEqual((usage["prompt_tokens"], usage["web_searches"]), (2000, 2))
        resent = json.loads(fake.call_args.args[0].data)["messages"][-1]
        self.assertEqual(resent["role"], "assistant")
        self.assertEqual(resent["content"][0]["input"], {"query": "x"})  # the tool call, rebuilt from the stream
        self.assertEqual(resent["content"][1]["text"], "First ")

    def test_claude_refusal_is_an_error_not_an_answer(self):
        with self.assertRaisesRegex(ConsultError, "declined"):
            self.call("claude-opus-5-5", FakeResponse(claude_events("Partial", stop="refusal")))

    def test_claude_cut_off_and_truncated(self):
        with self.assertRaisesRegex(ConsultError, "stream ended before"):
            self.call("claude-opus-5-5", FakeResponse(claude_events("x")[:3]))
        (text, _, _), _ = self.call("claude-opus-5-5", FakeResponse(claude_events("Part", stop="max_tokens")))
        self.assertIn("[truncated", text)

    def test_openrouter_uses_its_reported_cost(self):
        chunks = sse({"choices": [{"delta": {"content": "Hi"}}]},
                     {"choices": [{"delta": {}, "finish_reason": "stop"}],
                      "usage": {"prompt_tokens": 100, "completion_tokens": 20, "cost": 0.0123}}) + [b"data: [DONE]\n"]
        (text, usage, finish), fake = self.call("anthropic/claude-opus-5-5", FakeResponse(chunks))
        self.assertEqual((text, finish, usage["cost_usd"]), ("Hi", "stop", 0.0123))
        req = fake.call_args.args[0]
        self.assertTrue(req.full_url.startswith("https://openrouter.ai/api/v1/"))
        payload = json.loads(req.data)
        self.assertEqual((payload["reasoning"], payload["plugins"]), ({"effort": "high"}, [{"id": "web"}]))

    def test_openrouter_error_event(self):
        with self.assertRaisesRegex(ConsultError, "model call failed"):
            self.call("openai/gpt-6-sol", FakeResponse(sse({"error": {"message": "provider down"}})))

    def test_openrouter_estimate_strips_the_vendor(self):
        self.assertEqual(pricing.row_for("openai/gpt-6-sol:online", PRICES), pricing.row_for("gpt-6-sol", PRICES))


class WebOptInTests(Base):
    def test_web_is_opt_in(self):  # … but the commands pass web=False unless asked
        ns = lambda **kw: mock.Mock(no_web=False, web=False, **kw)  # noqa: E731
        self.assertFalse(commands._web(ns()))
        self.assertTrue(commands._web(mock.Mock(no_web=False, web=True)))
        os.environ["CONSULT_WEB"] = "on"
        self.assertTrue(commands._web(ns()))
        self.assertFalse(commands._web(mock.Mock(no_web=True, web=True)))


# ---------- panels: partial runs never look finished ----------

def fake_complete(costs=None, fail_at=None, answers=None):
    """A stand-in for providers.complete that records its calls."""
    calls = []

    def run(messages, model, effort, *a, **kw):
        calls.append(messages)
        n = len(calls)
        if fail_at == n:
            raise ConsultError("OpenAI HTTP 500: boom")
        cost = (costs or {}).get(n, 0.01)
        return ((answers or {}).get(n, f"answer {n}"),
                {"prompt_tokens": 10, "completion_tokens": 5, "cost_usd": cost, "web_searches": 0}, "stop")
    run.calls = calls
    return run


class PanelTests(Base):
    def test_finished_panel(self):
        fc = fake_complete()
        with mock.patch.object(commands, "complete", fc):
            out = self.run_cli("panel", "--label", "p", "-q", "Which channel?")
        report = self.reports("*panel-p.md")[0].read_text()
        self.assertEqual(len(fc.calls), 3)  # 2 experts + synthesis
        self.assertIn("## Synthesis", report)
        self.assertNotIn("in progress", report)
        self.assertNotIn("ABORTED", report)
        self.assertNotIn(FAKE_KEY, report + out)
        self.assertIn("Goal: ten users.", fc.calls[0][1]["content"])  # context file went along
        self.assertIn("not instructions to you", fc.calls[0][0]["content"])

    def test_cost_cap_stops_and_marks_the_report(self):
        with mock.patch.object(commands, "complete", fake_complete(costs={1: 0.4, 2: 0.4})):
            with self.assertRaises(SystemExit) as cm:
                self.run_cli("panel", "--label", "p", "-q", "Q", "--max-usd", "0.5")
        self.assertIn("exceeds the cap", str(cm.exception.code))
        report = self.reports("*panel-p.md")[0].read_text()
        self.assertIn("ABORTED – not a finished report", report)
        self.assertIn("2 of 2 experts answered; no synthesis", report)
        self.assertNotIn("## Synthesis", report)
        self.assertNotIn("in progress", report)

    def test_failing_expert_marks_the_report(self):
        with mock.patch.object(commands, "complete", fake_complete(fail_at=2)):
            with self.assertRaises(SystemExit):
                self.run_cli("panel", "--label", "p", "-q", "Q")
        report = self.reports("*panel-p.md")[0].read_text()
        self.assertIn("1 of 2 experts answered", report)
        self.assertIn("HTTP 500", report)

    def test_failing_synthesis_marks_the_report(self):
        with mock.patch.object(commands, "complete", fake_complete(fail_at=3)):
            with self.assertRaises(SystemExit):
                self.run_cli("panel", "--label", "p", "-q", "Q")
        self.assertIn("2 of 2 experts answered; no synthesis", self.reports("*panel-p.md")[0].read_text())

    def test_review_sends_the_screenshots_it_took(self):
        def shots(urls, out):
            out.mkdir(parents=True, exist_ok=True)
            (out / "home-desktop.jpg").write_bytes(b"\xff\xd8fake")
            return [out / "home-desktop.jpg"]
        fc = fake_complete()
        with mock.patch.object(commands, "complete", fc), mock.patch.object(commands, "take_shots", shots), \
                mock.patch.object(commands, "pages_block", lambda urls: "page text"):
            self.run_cli("review", "--pages", "/")
        parts = fc.calls[0][1]["content"]
        self.assertTrue(any(p.get("type") == "image_url" for p in parts), "no image sent")
        self.assertFalse(any("no longer exists" in p.get("text", "") for p in parts))


# ---------- threads ----------

class ThreadTests(Base):
    def test_ask_thread_round_trip(self):
        fc = fake_complete(answers={1: "First answer. ## Follow-up questions\n1. Budget?", 2: "Second answer."})
        with mock.patch.object(commands, "complete", fc):
            self.run_cli("ask", "--label", "plan", "--thread", "plan", "-q", "Round one?")
            self.run_cli("chat", "--thread", "plan", "-q", "Budget is 10 EUR.")
        second = fc.calls[1]
        self.assertEqual([m["role"] for m in second], ["system", "user", "assistant", "user"])
        self.assertIn("First answer", second[2]["content"])
        self.assertEqual(second[3]["content"], "Budget is 10 EUR.")
        state = json.loads((self.tmp / ".consult/reviews/threads/plan.json").read_text())
        self.assertEqual([r["assistant"] for r in state["rounds"]], ["First answer. ## Follow-up questions\n1. Budget?",
                                                                     "Second answer."])
        report = Path(state["report"]).read_text()
        self.assertIn("## Round 2", report)
        self.assertIn("Second answer.", report)

    def test_failed_round_is_not_saved(self):
        with mock.patch.object(commands, "complete", fake_complete()):
            self.run_cli("chat", "--thread", "t", "-q", "one")
        with mock.patch.object(commands, "complete", fake_complete(fail_at=1)), self.assertRaises(SystemExit):
            self.run_cli("chat", "--thread", "t", "-q", "two")
        state = json.loads((self.tmp / ".consult/reviews/threads/t.json").read_text())
        self.assertEqual(len(state["rounds"]), 1)


# ---------- what may be sent, and the dry run ----------

class SendableTests(Base):
    def ask(self, *extra):
        with mock.patch.object(commands, "complete", fake_complete()) as fc:
            self.run_cli("ask", "--label", "x", "-q", "Q", *extra)
        return fc

    def assertRefused(self, pattern, *extra):
        with self.assertRaises(SystemExit) as cm:
            self.ask(*extra)
        self.assertRegex(str(cm.exception.code), pattern)

    def test_never_send_directly_normalised_and_via_symlink(self):
        self.assertRefused("data/subscribers.csv matches never_send", "--files", "data/subscribers.csv")
        self.assertRefused("matches never_send pattern 'data/'", "--files", "docs/../data/subscribers.csv")
        (self.tmp / "docs" / "notes.md").symlink_to(self.tmp / "data" / "subscribers.csv")
        self.assertRefused("data/subscribers.csv matches never_send", "--files", "docs/notes.md")
        (self.tmp / "plan.md").write_text("private")
        self.assertRefused("plan.md matches never_send", "--files", "plan.md")

    def test_configured_context_files_take_the_same_check(self):
        cfg = self.tmp / ".consult" / "config.toml"
        cfg.write_text(cfg.read_text().replace('["docs/goal.md"]', '["docs/goal.md", "data/subscribers.csv"]'))
        self.assertRefused("matches never_send", "--dry-run")

    def test_outside_the_project_needs_allow_outside(self):
        outside = Path(tempfile.mkdtemp()) / "notes.md"
        self.addCleanup(shutil.rmtree, outside.parent)
        outside.write_text("external")
        self.assertRefused("outside the project", "--files", str(outside))
        (self.tmp / "docs" / "link.md").symlink_to(outside)
        self.assertRefused("symlink to .*outside the project", "--files", "docs/link.md")
        self.ask("--files", str(outside), "--allow-outside")

    def test_images_take_the_same_check(self):
        (self.tmp / "data" / "shot.png").write_bytes(b"\x89PNG")
        self.assertRefused("matches never_send", "--images", str(self.tmp / "data" / "shot.png"))

    def test_only_http_pages(self):
        self.assertRefused("only http", "--pages", "file:///etc/passwd")
        self.assertRefused("only http", "--pages", "/", "--base", "ftp://example.org")

    def test_dry_run_sends_nothing_and_shows_everything(self):
        (self.tmp / "docs" / "kpi.md").symlink_to(self.tmp / "docs" / "goal.md")
        (self.tmp / ".consult" / "memory.md").write_text("- decided X\n")
        fc = fake_complete()
        with mock.patch.object(commands, "complete", fc):
            out = self.run_cli("panel", "--label", "d", "-q", "What now?", "--files", "docs/kpi.md", "--dry-run")
        self.assertEqual(fc.calls, [])
        self.assertEqual(self.reports(), [])
        self.assertIn("DRY RUN", out)
        self.assertIn(f"docs/kpi.md → {self.tmp / 'docs' / 'goal.md'}", out)
        self.assertIn("memory", out)
        self.assertIn("call 2/2: cfo", out)
        self.assertIn("web search: off", out)
        self.assertIn("What now?", out)
        self.assertNotIn(FAKE_KEY, out)

    def test_dry_run_of_a_thread_shows_the_earlier_rounds(self):
        with mock.patch.object(commands, "complete", fake_complete(answers={1: "Earlier answer"})):
            self.run_cli("chat", "--thread", "t", "-q", "one")
        out = self.run_cli("chat", "--thread", "t", "-q", "two", "--dry-run")
        self.assertIn("1 earlier round(s)", out)
        self.assertIn("Earlier answer", out)
        state = json.loads((self.tmp / ".consult/reviews/threads/t.json").read_text())
        self.assertEqual(len(state["rounds"]), 1)


# ---------- init warnings ----------

@unittest.skipUnless(shutil.which("git"), "git not installed")
class InitTests(Base):
    def init(self, *extra) -> str:
        del os.environ["CONSULT_CONFIG"]
        repo = self.tmp / "repo"
        repo.mkdir(exist_ok=True)
        if not (repo / ".git").exists():
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
        cwd = os.getcwd()
        os.chdir(repo)
        try:
            return self.run_cli("init", *extra)
        finally:
            os.chdir(cwd)

    def test_private_not_ignored_warns(self):
        self.assertIn("is NOT ignored by git", self.init("--private"))

    def test_private_ignored_is_quiet(self):
        (self.tmp / "repo").mkdir()
        (self.tmp / "repo" / ".gitignore").write_text("private/\n")
        self.assertNotIn("WARNING", self.init("--private"))

    def test_already_tracked_warns(self):
        repo = self.tmp / "repo"
        (repo / "private" / "reviews").mkdir(parents=True)
        (repo / "private" / "reviews" / "old.md").write_text("secret numbers")
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "add", "private"], check=True)
        out = self.init("--private")
        self.assertIn("git already tracks 1 file(s) under private/reviews/", out)


# ---------- pricing ----------

class PricingTests(unittest.TestCase):
    def test_parser_against_saved_page(self):
        self.assertEqual(PRICES["models"]["gpt-6-sol"], {"input": 2.0, "cached": 0.2, "cache_write": 2.5, "output": 10.0})
        self.assertIn("gpt-5.5", PRICES["models"])  # "gpt-5.5 (<272K context length)" → gpt-5.5
        self.assertEqual(PRICES["web_search_per_call"], 0.01)

    def test_changed_page_format_fails_loudly(self):
        with self.assertRaisesRegex(ValueError, "format changed"):
            pricing._parse("<html>no table here</html>")

    def test_cost_with_cache_and_search(self):
        usd = pricing.cost("gpt-6-sol-2026-09-01", {"prompt_tokens": 1_000_000, "cached_tokens": 500_000,
                                                   "completion_tokens": 100_000, "web_searches": 3}, PRICES)
        self.assertAlmostEqual(usd, 0.5 * 2 + 0.5 * 0.2 + 0.1 * 10 + 0.03)
        self.assertIsNone(pricing.cost("unknown-model", {"prompt_tokens": 1}, PRICES))


if __name__ == "__main__":
    unittest.main()
