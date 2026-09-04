"""Message rendering under DLT template constraints (Track 03: Hinglish recovery).

THE POINT OF THIS MODULE
spec §1 gives the LLM two jobs: root-cause synthesis, and drafting
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

Nothing here is ever sent (spec §2). Rendered text is logged.
"""

from __future__ import annotations

import re
import unicodedata
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
    """Raised when copy is not a registered template with valid slots. Never suppressed."""


# Per-slot constraints. DLT variable fields are SUBSTITUTIONS - a name, an amount, a
# merchant - not free prose. Without these, matching only the template SHAPE lets
# arbitrary text ride into a customer message inside a slot: the pattern still matches,
# because slots are wildcards, and the result is both a TRAI breach and a phishing
# vector. Found by red-teaming our own compliance claim.
#
#   render(..., merchant="Acme</p> IGNORE THE ABOVE. Pay now or we contact your
#                         employer. Click evil.com")
#
# produced a message that verify_compliance happily certified as template FUNDS.
SLOT_RULES: dict[str, dict] = {
    "name":     {"max_len": 48,  "allow_url": False},
    "amount":   {"max_len": 24,  "allow_url": False},
    "merchant": {"max_len": 48,  "allow_url": False},
    "rail":     {"max_len": 20,  "allow_url": False},
    "days":     {"max_len": 5,   "allow_url": False},
    "link":     {"max_len": 128, "allow_url": True},
}
DEFAULT_SLOT_RULE = {"max_len": 48, "allow_url": False}

_URLISH = re.compile(r"https?://|www\.|\.[a-z]{2,}/|\.(com|net|org|io|co|in)\b", re.I)

# ALLOW-LIST, not a deny-list. A deny-list is a losing game: our first version rejected
# markup, URLs and control characters, and a short SQL-shaped string sailed through it
# into a customer message - no URL, no angle brackets, under the length cap. Harmless to
# the database (queries are parameterised) but it has no business in an SMS.
#
# Classified by UNICODE CATEGORY rather than by a character range, because a regex
# allow-list written in ASCII habits gets India wrong twice over: `\w` does not match
# combining marks, so "मीरा" is rejected on its vowel signs, and it does not match
# currency symbols, so an amount of "Rs 2,499" formatted with the rupee sign is rejected
# too. Both were live false positives on the first attempt.
_ALLOWED_CATEGORIES = frozenset({
    "Lu", "Ll", "Lt", "Lm", "Lo",   # letters, every script
    "Mn", "Mc", "Me",               # combining marks - essential for Indic scripts
    "Nd", "Nl", "No",               # numbers
    "Zs",                           # spaces
})

# Currency symbols are NOT allowed as a whole Unicode category. Doing that admits "$",
# which let a shell-shaped payload through. Only the symbols our shipped markets
# actually use are permitted, so the allow-list narrows itself as configuration does.


@lru_cache(maxsize=1)
def _allowed_currency_symbols() -> frozenset:
    from src.market import get_market, known_markets
    return frozenset(get_market(c).currency.symbol for c in known_markets())
# Underscore is here because merchant identifiers legitimately contain it
# ("merchant_demo"). It is inert in a DLT template: it is not a delimiter, not a
# control character, and _UNSAFE below still blocks the injection vectors that
# matter (angle brackets, braces, backslash, newlines, bidi overrides).
# Omitting it silently rejected EVERY message in the batch - see BUILD_NOTES.
_ALLOWED_PUNCTUATION = frozenset(".,&'()-/+_\u2019")


def _is_inert(text: str) -> bool:
    permitted = _ALLOWED_PUNCTUATION | _allowed_currency_symbols()
    return all(unicodedata.category(ch) in _ALLOWED_CATEGORIES or ch in permitted
               for ch in text)
_UNSAFE = re.compile(r"[<>{}\\\r\n\t\x00-\x1f\u202a-\u202e\u2066-\u2069]")


def validate_slot(name: str, value: str) -> str:
    """A slot value must be a short, inert substitution. Raises rather than sanitising.

    Silently stripping bad input would let a caller believe their value was sent as
    written. Refusing makes the boundary explicit at the point it is crossed.
    """
    text = str(value)
    rule = SLOT_RULES.get(name, DEFAULT_SLOT_RULE)
    if len(text) > rule["max_len"]:
        raise TemplateViolation(
            f"slot {name!r} is {len(text)} chars; registered templates allow "
            f"{rule['max_len']}. A slot is a substitution, not a message.")
    if _UNSAFE.search(text):
        raise TemplateViolation(
            f"slot {name!r} contains markup, control or bidi characters")
    if not rule["allow_url"] and _URLISH.search(text):
        raise TemplateViolation(
            f"slot {name!r} contains a URL; only the link slot may carry one")
    if not rule["allow_url"] and not _is_inert(text):
        raise TemplateViolation(
            f"slot {name!r} contains characters outside the permitted set for a "
            "substitution field")
    return text


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


def can_render(failure_class: FailureClass, channel: Channel,
               source_type: str = "payment") -> bool:
    """Is there a registered template that could actually carry this message?

    The action space must contain only actions the system can perform. Offering a nudge
    on a channel with no registered template means the agent spends a decision on
    something that cannot happen - and, before this was enforced, the episode was still
    charged for it and still scored as though a message had gone out.
    """
    key = template_for(failure_class, source_type)
    if key is None:
        return False
    return channel.value in load_templates()[key]["channels"]


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

    # Validate every slot BEFORE substitution. Checking only the rendered shape is not
    # enough: slots are wildcards in that pattern, so anything placed in one matches.
    safe = {s: validate_slot(s, slots[s]) for s in tpl["slots"]}
    text = tpl["variants"][lang].format(**safe)
    return RenderedMessage(key, tpl["dlt_id"], lang, channel.value, text, safe)


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
