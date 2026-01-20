# What Lotus Is (and Is Not)

This document exists to set **intent, boundaries, and expectations**.

Lotus is deliberately small, neutral, and open. If this document feels opinionated, that is intentional.

---

## What Lotus Is

**Lotus is an open semantic substrate.**

It defines:
- how things are described (artifacts, relations, features)
- how systems are queried (Lotus Queries)
- how computation is exposed (capabilities)
- how tools, agents, and UIs discover what is possible (introspection)

Lotus is:
- local-first
- introspectable by default
- extensible via plugins
- independent of transport (MCP, HTTP, CLI, UI)
- independent of implementation language

It is intended to be **shared infrastructure**, not a product.

---

## What Lotus Is Not

Lotus is **not**:
- an AI assistant
- an agent framework
- a hosted service
- a monetization platform
- a Copilot / Claude / ChatGPT competitor
- a company-controlled API

Those things can be built **on top of Lotus** as plugins or products.

---

## Zero AI by Design

Lotus contains:
- no models
- no inference engines
- no prompts
- no opinions about AI techniques

This is a constraint, not a limitation.

AI systems change quickly. Primitives should not.

Lotus exists to support many approaches to computation — AI-based or otherwise — without locking users into one worldview.

---

## Zero Money by Design

Lotus contains:
- no billing hooks
- no hosted tiers
- no telemetry requirements
- no growth mechanisms

Money distorts primitives.

Lotus is designed to remain usable by:
- individuals
- researchers
- hobbyists
- small teams
- long-lived community projects

---

## Infrastructure Commons

Lotus is closer to:
- GraphQL (introspection + schema-driven tooling)
- LLVM (intermediate representation)
- POSIX (shared contracts)
- OpenTelemetry (neutral observability primitives)

It is **not** closer to:
- proprietary AI platforms
- vertically integrated developer tools
- closed SaaS products

---

## Relationship to Products (e.g. Embeddr)

Products may:
- embed Lotus
- extend Lotus via plugins
- build UX and workflows on top
- protect their own product IP

Lotus itself must remain:
- open
- forkable
- community-modifiable
- implementation-agnostic

If Lotus were closed, downstream products would lose trust in the foundation.

---

## Open Source Philosophy

Lotus is intended to be:
- openly developed
- permissively licensed
- modified and reimplemented by the community

The goal is not control.

The goal is **shared language**.

---

## The Role of the Community

The community is expected to:
- experiment
- tinker
- replace components
- write plugins in many languages
- disagree on implementations

Lotus exists so those disagreements do not fragment the ecosystem.

---

## One Sentence Summary

> **Lotus defines shared primitives for describing, querying, and invoking computation — and deliberately stays out of everything else.**

---

## If You Are Unsure Where Something Belongs

Ask:
1. Does this define meaning, or just execution?
2. Would this still make sense in 10 years?
3. Could someone else build this better in another language?

If the answer points away from core, it belongs in a plugin.

