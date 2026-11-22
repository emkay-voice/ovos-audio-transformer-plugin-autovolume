"""The control panel's page: every message it sends is one the plugin answers,
every string is there in every language, and the files go in the wheel."""
import json
import re
from pathlib import Path

ROOT = Path(__file__).parent.parent
PKG = ROOT / "ovos_audio_transformer_plugin_autovolume"
PAGE = json.loads((PKG / "controlpanel.json").read_text())
LANGS = ["en-US", "da-DK", "de-DE", "nl-NL", "fr-FR", "es-ES", "it-IT",
         "pt-BR", "pt-PT", "ca-ES", "gl-ES", "eu-ES"]
HANDLED = {"ovos.autovolume." + m for m in ("status", "enable", "disable", "forget", "reload")}


def walk(o):
    if isinstance(o, dict):
        for k, v in o.items():
            yield k, v
            yield from walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from walk(v)


def test_every_message_is_handled():
    sent = {v for k, v in walk(PAGE) if k in ("query", "send", "reload")}
    assert sent and sent <= HANDLED


def test_every_label_in_every_language():
    labels = {v for k, v in walk(PAGE) if k in ("title", "text", "label", "heading", "hint", "confirm", "done")
              and isinstance(v, str) and not v.startswith("{")}
    en = json.loads((PKG / "locale" / "en-US" / "controlpanel.json").read_text())
    assert labels <= set(en)
    for lang in LANGS:
        d = json.loads((PKG / "locale" / lang / "controlpanel.json").read_text())
        assert set(d) == set(en), lang
        for k, v in d.items():   # the same values in the same places
            assert set(re.findall(r"{\w+}", v)) == set(re.findall(r"{\w+}", en[k])), (lang, k)


def test_the_files_go_in_the_wheel():
    pyproject = (ROOT / "pyproject.toml").read_text()
    for f in ("controlpanel.json", "controlpanel.svg", "locale/*/*.json"):
        assert f'"{f}"' in pyproject
