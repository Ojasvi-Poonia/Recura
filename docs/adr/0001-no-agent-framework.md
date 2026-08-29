# ADR 0001: No agent framework

**Status:** Accepted · **Date:** 2026-08-27

## Context

The obvious way to build an agent in 2026 is to reach for LangChain, LangGraph, CrewAI
or the Claude Agent SDK. They supply the loop, tool dispatch, retries and state, and
they are genuinely good at that.

Recura has three requirements that pull against them:

1. **Byte-identical replay.** `make eval` must produce the same numbers on every run, on
   any machine, forever. That requires control over every source of non-determinism —
   RNG streams, iteration order, model calls, clock reads.
2. **A control plane the model cannot reach.** The policy engine must be provably outside
   the model's context. A framework that manages the loop also manages what the model
   sees, and "provably" becomes hard to assert.
3. **A loop someone can read in an interview.** The decision procedure *is* the
   submission. If a reviewer cannot follow it top to bottom, the argument does not land.

## Decision

Hand-roll the loop. `src/agent.py` is 525 lines, 391 of them code, with `run_episode`
being 200 lines of explicit control flow.

An AST-parsing test fails the build if `langchain`, `langgraph`, `crewai`, `autogen` or
`llama_index` is imported anywhere under `src/` or `eval/`.

## Consequences

**Good.** Every random draw is threaded through an explicitly seeded generator, so
Thompson sampling — a stochastic algorithm — produces identical eval runs. The policy
engine is a separate module that `src/decide/` is forbidden by test from importing. The
five steps of the loop map one-to-one onto five blocks of code.

**Bad.** We wrote and debugged retry handling, idempotency, fallback and state
management ourselves. Framework users get those free. We also forgo the ecosystem —
tracing, evals, integrations — and would have to build any of it we later wanted.

**Neutral.** The loop is small enough that "just add a framework later" remains
available. Nothing here is load-bearing enough to be hard to replace.

## Alternatives considered

**LangGraph.** Best fit on paper — explicit graph, checkpointing. Rejected because
checkpointing is not the same as determinism, and the graph abstraction would sit between
us and the RNG.

**Claude Agent SDK.** Batteries-included and well-built, but it supplies a *harness* and
we would still host and deploy it. It also assumes the model drives the loop, which is
precisely the arrangement we are arguing against: here the maths drives and the model
advises.

**A thin tool-runner.** Tempting, but our loop makes at most one model call per decision
and no tool calls at all. There is no dispatch problem to solve.
