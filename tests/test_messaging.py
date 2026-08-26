"""DLT template compliance and Hinglish rendering (Track 03: Hinglish voice recovery)."""

import pytest

from src.act import voice as voice_mod
from src.act.messaging import (
    LANGUAGES,
    RenderedMessage,
    TemplateViolation,
    load_templates,
    render,
    template_for,
    verify_compliance,
    voice_script,
)
from src.models import Channel, FailureClass

SLOTS = {"name": "Priya", "amount": "Rs 2,499", "merchant": "Acme",
         "link": "https://rzp.io/i/x", "rail": "UPI", "days": "45"}


# --- the compliance boundary ------------------------------------------------

def test_free_form_copy_is_refused():
    """The whole safety property: unregistered text cannot be treated as sendable.

    TRAI requires every commercial message to match a registered DLT template. If a
    model could emit arbitrary text into a customer's inbox, that is a breach - so
    anything that cannot be matched back to a registered pattern raises.
    """
    with pytest.raises(TemplateViolation):
        verify_compliance("Pay immediately or we will contact your employer.")


def test_every_rendered_message_verifies_back_to_its_template():
    for cls in (FailureClass.FUNDS, FailureClass.AUTH_ABANDON,
                FailureClass.INSTRUMENT_INVALID, FailureClass.TRANSIENT_INFRA,
                FailureClass.LIMIT_EXCEEDED):
        for lang in ("en", "hi"):
            msg = render(cls, lang, Channel.SMS, SLOTS)
            assert verify_compliance(msg.text, lang) == msg.template_key


def test_risk_declines_have_no_template_at_all():
    """We do not message a customer about an issuer risk decline."""
    assert template_for(FailureClass.RISK_DECLINE) is None
    with pytest.raises(TemplateViolation):
        render(FailureClass.RISK_DECLINE, "en", Channel.SMS, SLOTS)


def test_unknown_diagnosis_sends_nothing():
    """We do not assert a cause we could not determine."""
    assert template_for(FailureClass.UNKNOWN) is None


def test_missing_slots_raise_rather_than_render_a_gap():
    with pytest.raises(TemplateViolation):
        render(FailureClass.FUNDS, "en", Channel.SMS, {"name": "Priya"})


def test_unregistered_channel_is_refused():
    templates = load_templates()
    templates["FUNDS"]["channels"] = ["sms"]
    try:
        with pytest.raises(TemplateViolation):
            render(FailureClass.FUNDS, "en", Channel.EMAIL, SLOTS)
    finally:
        templates["FUNDS"]["channels"] = ["sms", "whatsapp", "voice"]


# --- Hinglish ---------------------------------------------------------------

def test_hinglish_is_latin_script_not_devanagari():
    """Hinglish is code-mixing in LATIN script - what people actually read, and what
    renders consistently across handsets."""
    text = render(FailureClass.FUNDS, "hi", Channel.SMS, SLOTS).text
    assert not any("ऀ" <= ch <= "ॿ" for ch in text), text
    assert "aapka" in text or "ka payment" in text


def test_devanagari_variant_exists_for_voice():
    text = render(FailureClass.FUNDS, "deva", Channel.VOICE, SLOTS).text
    assert any("ऀ" <= ch <= "ॿ" for ch in text)


def test_unknown_language_falls_back_to_english():
    assert render(FailureClass.FUNDS, "ta", Channel.SMS, SLOTS).language == "en"


def test_every_template_has_every_language():
    for key, tpl in load_templates().items():
        assert set(tpl["variants"]) == set(LANGUAGES), key


def test_every_template_declares_a_dlt_id():
    for key, tpl in load_templates().items():
        assert str(tpl["dlt_id"]).isdigit() and len(str(tpl["dlt_id"])) >= 15, key


def test_receivables_have_their_own_template():
    assert template_for(FailureClass.UNKNOWN, source_type="invoice") == "RECEIVABLE"
    msg = render(FailureClass.UNKNOWN, "hi", Channel.SMS, SLOTS, source_type="invoice")
    assert "invoice" in msg.text and "45" in msg.text


# --- voice ------------------------------------------------------------------

def test_voice_script_removes_urls():
    """A URL read aloud is noise."""
    script = voice_script(render(FailureClass.FUNDS, "en", Channel.VOICE, SLOTS))
    assert "http" not in script and "rzp.io" not in script


def test_voice_script_leaves_no_dangling_deictic():
    """Stripping the link must not leave 'Here.' pointing at nothing."""
    for lang in ("en", "hi"):
        script = voice_script(render(FailureClass.FUNDS, lang, Channel.VOICE, SLOTS))
        assert not script.rstrip(".").endswith(("Yahan", "here", "Here"))
        assert script.endswith((".", "।"))


def test_devanagari_script_does_not_get_double_punctuation():
    script = voice_script(render(FailureClass.FUNDS, "deva", Channel.VOICE, SLOTS))
    assert not script.endswith("।.")


def test_latin_hinglish_uses_an_indian_english_voice():
    """A Devanagari voice fed Latin-script Hinglish produces nonsense."""
    assert voice_mod.VOICE_BY_LANGUAGE["hi"] == voice_mod.VOICE_BY_LANGUAGE["en"]
    assert voice_mod.VOICE_BY_LANGUAGE["deva"] != voice_mod.VOICE_BY_LANGUAGE["hi"]


def test_synthesis_degrades_without_an_engine(monkeypatch, tmp_path):
    """Audio is a demo-only optional dependency; the script is always produced."""
    monkeypatch.setattr(voice_mod, "engine_available", lambda: False)
    clip = voice_mod.synthesise(
        render(FailureClass.FUNDS, "hi", Channel.VOICE, SLOTS), out_dir=tmp_path)
    assert clip.synthesised is False
    assert clip.script and clip.path is None
