# Worked example: one consultation, start to finish

A synthetic project, a real run. Everything about "Sourdough Club" is invented; the model call, its output, the
tokens and the cost are real. It shows what `consult` produces and – more important – what the person and the
agent do with it: accept, reject, decide, record.

## The project (synthetic)

Files in [example/](example/): the config, `goal.md` and `kpi.md` exactly as they were attached.

*Sourdough Club* is a weekly recipe email with 312 free readers, growing by 18 a week, no paid offer. The goal:
one paying subscriber within 60 days, with the operator spending at most two hours a week. Two routes on the
table: launch a €6/month tier now (A) or grow the free list to 1,000 with a lead magnet first (B).

## The question

```
consult ask --label "paid tier now or grow first" --persona pricing --effort medium \
  -q "Route A (launch the €6/month paid tier now) or route B (grow the free list to 1,000 with a lead magnet,
      then launch)? Give the arithmetic for each route with every number tagged measured / benchmark / guess,
      the expected time to the first paying subscriber, what could make either route fail, and one concrete
      first step for the coming week. If you see a third route that fits the 2 h/week limit, name it."
```

Persona `pricing` (a reusable one from `personas/`), model `gpt-6-sol`, reasoning effort medium, web search on.
The system prompt = persona title + instructions + the project's framing; the user message = `goal.md`, `kpi.md`
and the question. The full run, verbatim: [example/report.md](example/report.md).

**Run data:** 64 s wall clock · 13,640 input / 2,806 output tokens (1,731 of them hidden reasoning) · 2 web
searches · **cost ≈ $0.08** at list prices of 2026-09-28 (estimate: tokens × published prices + $0.01 per search).

## What came back (abridged)

Recommendation: **route A now.** The arithmetic: at the measured 18 readers/week, route B reaches 1,000 readers
around June 2027 – 40 weeks after a 60-day deadline; to make it in time B would need 80 new readers a week, "a
guess, not a plan supported by the log". Route A: 312 readers × a 2–5 % *benchmark* conversion = 6–16 subscribers,
explicitly labelled "benchmark-based illustration, not a forecast". It proposed packaging (weekly recipe stays free;
the 40-recipe archive plus a monthly group Q&A email for €6), a time budget (≈ 90 min/week, leaving 30 of the 120),
failure conditions for both routes, a **third route** (test the same €6 offer on the two readers who asked to pay
plus one who asked about dense crumb – price testing, not a product), and a first step for the week with the EU
withdrawal-rule reminder and a Stripe fee illustration it found on the web.

## What the operator does with it

*In this synthetic case the decision below was written by the agent to show the format; in a real project this
paragraph is the human's, and it is what makes the report worth keeping.*

**Accepted:** route A; the staged version (three named prospects first); "count completed payments, not positive
replies"; the archive-readiness check before calling anything launched.

**Rejected, with reasons:**
- The 2–5 % conversion benchmark as a planning number. Two people asking "how can I pay" is the only measured
  demand signal; the plan is written for **2 → 1** (two prospects, one payment), not for 6–16 subscribers.
- The monthly group Q&A email as part of the launch package. It adds a recurring obligation before the first euro
  exists; it can be added once someone pays. (The consultant's own time budget shows the margin is thin.)
- The Stripe figures: a web-found illustration for Ireland, tagged as such by the consultant – fees must be
  checked for the operator's own country and provider before any price is printed.

**Decision:** verify the archive this week; set up one checkout link with the legally required information;
message the two readers who asked; report the first result (a payment, or two "no"s and why) in the KPI log.
Recorded for future consultations: `consult memory add "2026-09-28: launched €6 tier as a staged test with 2
named prospects; group Q&A deferred until the first payment"`.

## Why this shape

- **Inputs are reproducible.** Model, effort, files, system prompt and question are in the report header; a
  second run with another persona (`--no-web`, no prior report attached) can be compared honestly.
- **Numbers carry their provenance.** measured / benchmark / guess appears in the question and is demanded in
  the answer, so a "benchmark-based illustration" cannot quietly become a forecast.
- **The human's judgement is visible.** The rejected items are the part a reader learns most from – and the part
  a model cannot produce.
- **The cost is on the page.** $0.08 for this; a four-persona panel on the flagship for a publish/no-publish
  decision cost $3.84 the same day. Both were worth it; neither should be invisible.
