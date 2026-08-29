# ADR 0010: The model fills registered templates; it never writes copy

**Status:** Accepted · **Date:** 2026-08-27

## Context

The spec gives the LLM two jobs: root-cause synthesis, and drafting customer-facing copy.
We built the first, measured it, and found it wanting (ADR 0005). The second is where a
language model is genuinely strong — producing natural copy in a register a person will
actually read, which for India means Hinglish.

But an LLM writing customer messages in India is not merely risky. TRAI requires every
commercial message to match a template registered on the DLT platform, with variable
fields matching what is actually sent. Free-form copy is a **breach**, and mixing
promotional content into a transactional message reclassifies the whole message and pulls
it under DND and the time window.

So the interesting question is not "can the model write good Hinglish". It is "how do you
let a model near customer communication at all".

## Decision

The same trick as the policy gate, applied to language.

`config/templates.yaml` holds six DLT-registered templates, each with a registration id,
permitted channels, declared slots, and `en` / `hi` / `deva` variants. The model **selects
a template and fills its slots**, and nothing else.

`verify_compliance()` re-derives the template from rendered text by turning each registered
pattern into a regex. Anything that cannot be matched back raises `TemplateViolation`.

`RISK_DECLINE` and `UNKNOWN` have **no template at all** — we do not message someone about
an issuer risk decline, and we do not assert a cause we could not determine.

## Consequences

**"The LLM never authors free-form copy" is a property a test checks.** Feeding it a
threat produces `TemplateViolation`, not a message.

**No message can be invented to fill a gap.** If no registered template covers a
diagnosis, the agent sends nothing rather than improvising.

**Hinglish is Latin script, deliberately.** `hi` here is Hindi-English code-mixing written
in Latin characters — what Indian consumers actually read, and what renders consistently
across handsets where Devanagari does not. A `deva` variant exists for voice, where script
is irrelevant and pronunciation is not.

**Voice selection is counter-intuitive and matters.** Latin-script Hinglish fed to a
Devanagari Hindi voice produces nonsense, because the voice expects Devanagari. An
**Indian English** voice reads it correctly, applying Indian phonology to Latin letters.
So `hi` uses the English voice and `deva` uses the Hindi one.

**We claim compliance, not lift.** We prove no unregistered copy can escape. We make **no
claim** that Hinglish improves recovery, because the frozen generator does not model
language matching. The capability is real; the benefit is unmeasured, and `RESULTS.md`
says so.

**Synthesis is an optional, demo-only dependency.** `make voice` renders samples where a
TTS engine exists and degrades to scripts where it does not. Nothing is ever dialled.

## Alternatives considered

**Let the model write the message.** What most demos do. Illegal in this jurisdiction, and
the compliance argument is the interesting part of the problem.

**Templates with no model involvement.** Safe and what we would ship at `w = 0`. The model
still earns its place by choosing among templates and filling slots naturally, which is
the part rules do badly.

**Devanagari-only Hindi.** Renders inconsistently on older handsets and is not how payment
messages are actually written in India.
