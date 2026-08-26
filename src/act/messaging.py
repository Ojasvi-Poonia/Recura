"""Message rendering under DLT template constraints (Track 03: Hinglish recovery).

THE POINT OF THIS MODULE
CLAUDE.md section 1 gives the LLM two jobs: root-cause synthesis, and drafting
customer-facing copy. Only the first was built - and we measured it (eval/calibration.py)
and found it wanting. This is the second, and it is the job a language model is actually
good at: producing natural copy in a register a person will read.

But it cannot be allowed to simply write. TRAI requires every commercial message to
match a template registered on DLT, with variable fields matching what is actually
sent. So the model fills SLOTS in a registered template and nothing else, and
`verify_compliance` re-derives the template from the rendered text. That turns "the LLM
never writes free-form copy" from a promise into something a test can check - which is
the same trick as the policy gate, applied to language instead of money.

HINGLISH, SPECIFICALLY
`hi` here means Hinglish: Hindi-English code-mixing in Latin script. It is what Indian
consumers actually read in payment messages, and Latin script renders consistently
across handsets where Devanagari does not. A `deva` variant exists for voice, where
script is irrelevant and pronunciation is not.

Nothing here is ever sent (CLAUDE.md section 2). Rendered text is logged.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

from src.models import Channel, FailureClass

TEMPLATES_PATH = Path(__file__).resolve().parents[2] / "config" / "templates.yaml"
LANGUAGES = ("en", "hi", "deva")

# Which template serves which diagnosis. RISK_DECLINE and UNKNOWN are absent on
# purpose: we do not message a customer about a risk decline, and we do not send copy
# asserting a cause we could not determine.
CLASS_TO_TEMPLATE: dict[FailureClass, str] = {
    FailureClass.FUNDS: "FUNDS",
    FailureClass.AUTH_ABANDON: "AUTH_ABANDON",
    FailureClass.INSTRUMENT_INVALID: "INSTRUMENT_INVALID",
    FailureClass.TRANSIENT_INFRA: "TRANSIENT_INFRA",
    FailureClass.LIMIT_EXCEEDED: "LIMIT_EXCEEDED",
}


class TemplateViolation(Exception):
    """Raised when text does not match any registered template. Never suppressed."""


@dataclass(frozen=True)
class RenderedMessage:
    template_key: str
    dlt_id: str
    language: str
    channel: str
    text: str
    slots: dict[str, str]

    def as_log(self) -> dict:
        return {"template": self.template_key, "dlt_id": self.dlt_id,
                "language": self.language, "channel": self.channel,
                "text": self.text, "slots": self.slots}


@lru_cache(maxsize=1)
def load_templates(path: str | None = None) -> dict:
    with Path(path or TEMPLATES_PATH).open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def template_for(failure_class: FailureClass, source_type: str = "payment") -> str | None:
    """Which registered template applies. None means: do not message about this."""
    if source_type == "invoice":
        return "RECEIVABLE"
    return CLASS_TO_TEMPLATE.get(failure_class)


def render(
    failure_class: FailureClass,
    language: str,
    channel: Channel,
    slots: dict[str, str],
    source_type: str = "payment",
) -> RenderedMessage:
    """Fill a registered template. Raises rather than improvising."""
    key = template_for(failure_class, source_type)
    if key is None:
        raise TemplateViolation(
            f"no registered template for {failure_class.value}; this class must not "
            "generate customer contact")

    templates = load_templates()
    tpl = templates[key]
    if channel.value not in tpl["channels"]:
        raise TemplateViolation(f"template {key} is not registered for {channel.value}")

    lang = language if language in tpl["variants"] else "en"
    missing = [s for s in tpl["slots"] if s not in slots]
    if missing:
        raise TemplateViolation(f"template {key} requires slots {missing}")

    text = tpl["variants"][lang].format(**{s: slots[s] for s in tpl["slots"]})
    return RenderedMessage(key, tpl["dlt_id"], lang, channel.value, text,
                           {s: slots[s] for s in tpl["slots"]})


def verify_compliance(text: str, language: str = "en") -> str:
    """Re-derive which registered template produced this text.

    This is the enforcement point. Any string that cannot be matched back to a
    registered pattern is, by definition, free-form copy - and free-form commercial
    copy is a DLT breach. Raising here is the whole safety property.
    """
    for key, tpl in load_templates().items():
        pattern = tpl["variants"].get(language) or tpl["variants"]["en"]
        # Turn the template into a regex: literal text, slots become wildcards.
        regex = "^" + re.sub(r"\\\{(\w+)\\\}", r"(?P<\1>.+?)",
                             re.escape(pattern)) + "$"
        if re.match(regex, text, flags=re.DOTALL):
            return key
    raise TemplateViolation(
        "text does not match any registered DLT template - refusing to treat it as "
        "sendable copy")


# A URL cannot be read aloud, so every call-to-action that points at one is replaced
# with its spoken equivalent BEFORE the link is stripped. Doing it the other way round
# leaves dangling deictics - "Yahan." ("Here.") pointing at nothing.
_SPOKEN_CTA = (
    ("Yahan complete karein:", "Kripya app mein payment poora karein"),
    ("Yahan poora karein:", "Kripya app mein payment poora karein"),
    ("Yahan settle karein:", "Kripya app mein invoice settle karein"),
    ("Complete it here:", "Please complete it in the app"),
    ("Finish here:", "Please complete it in the app"),
    ("Settle here:", "Please settle it in the app"),
    ("It should work now:", "Please try again now"),
    ("It resets tomorrow:", "It resets tomorrow, please try again then"),
    ("Ab try karein:", "Kripya ab dobara try karein"),
    ("Kal reset hoga:", "Kal reset hoga, kripya tab try karein"),
)


def voice_script(message: RenderedMessage) -> str:
    """Spoken form. Links cannot be read aloud, so the call-to-action is respoken."""
    text = message.text
    for written, spoken in _SPOKEN_CTA:
        text = text.replace(written, spoken)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"\s*\{?link\}?", "", text)
    text = " ".join(text.split())
    # Devanagari sentences already end in a danda; do not append a second stop.
    text = text.rstrip(" .:,")
    return text if text.endswith("\u0964") else text + "."
