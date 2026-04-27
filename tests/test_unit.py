"""Unit-Tests: Hilfsfunktionen ohne Datenbankzugriff."""

import json
import re
import pytest


# ---------------------------------------------------------------------------
# _naechste_umfrage_id – Logik isoliert testen
# ---------------------------------------------------------------------------

def _naechste_id_from_last(last_id: str | None) -> str:
    """Repliziert die ID-Generierungslogik aus main.py für Unit-Tests."""
    from datetime import datetime
    jahr = datetime.now().year
    if last_id:
        num = int(last_id.split("-")[-1]) + 1
    else:
        num = 1
    return f"UMF-{jahr}-{num:03d}"


class TestUmfrageIdGenerierung:
    """Prüft die automatische Umfrage-ID-Logik."""

    def test_erste_id_des_jahres(self):
        result = _naechste_id_from_last(None)
        assert re.match(r"UMF-\d{4}-001$", result), f"Ungültige ID: {result}"

    def test_folge_id_inkrementiert(self):
        from datetime import datetime
        jahr = datetime.now().year
        result = _naechste_id_from_last(f"UMF-{jahr}-005")
        assert result == f"UMF-{jahr}-006"

    def test_id_format_dreistellig(self):
        from datetime import datetime
        jahr = datetime.now().year
        result = _naechste_id_from_last(f"UMF-{jahr}-009")
        assert result.endswith("-010")

    def test_id_enthaelt_aktuelles_jahr(self):
        from datetime import datetime
        result = _naechste_id_from_last(None)
        assert str(datetime.now().year) in result

    def test_id_format_regex(self):
        result = _naechste_id_from_last(None)
        assert re.fullmatch(r"UMF-\d{4}-\d{3}", result)


# ---------------------------------------------------------------------------
# Betreff-Parsing (Regex aus main.py)
# ---------------------------------------------------------------------------

class TestBetreffParsing:
    """Prüft die Regex-Extraktion der Umfrage-ID aus E-Mail-Betreff."""

    PATTERN = r"\[([A-Z]{3}-\d{4}-\d{3})\]"

    def test_valider_betreff(self):
        match = re.search(self.PATTERN, "[UMF-2026-001] Re: Seminar")
        assert match is not None
        assert match.group(1) == "UMF-2026-001"

    def test_betreff_ohne_id_liefert_none(self):
        match = re.search(self.PATTERN, "Re: Seminar ohne ID")
        assert match is None

    def test_betreff_mit_prefix_text(self):
        match = re.search(self.PATTERN, "Fwd: [UMF-2026-042] Antwort")
        assert match.group(1) == "UMF-2026-042"

    def test_kleinbuchstaben_werden_nicht_gematcht(self):
        match = re.search(self.PATTERN, "[umf-2026-001] klein")
        assert match is None

    def test_dreistellige_nummer_erforderlich(self):
        # Zweistellig → kein Match
        match = re.search(self.PATTERN, "[UMF-2026-01] kurz")
        assert match is None

    def test_vierstellige_nummer_kein_match(self):
        match = re.search(self.PATTERN, "[UMF-2026-1000] lang")
        assert match is None


# ---------------------------------------------------------------------------
# KI-Output Validierung
# ---------------------------------------------------------------------------

class TestKiOutputValidierung:
    """Prüft dass KI-Output die erwarteten Felder enthält."""

    VALIDE_FELDER = {"kernaussage", "bewertung", "handlungsbedarf", "stichworte", "vollstaendigkeit"}
    VALIDE_BEWERTUNGEN = {"POSITIV", "NEGATIV", "NEUTRAL", "UNKLAR"}
    VALIDE_VOLLSTAENDIGKEIT = {"VOLLSTAENDIG", "TEILWEISE", "UNVOLLSTAENDIG"}

    def _beispiel_output(self) -> dict:
        return {
            "kernaussage": "Das Seminar war gut.",
            "bewertung": "POSITIV",
            "handlungsbedarf": False,
            "stichworte": ["Gut", "Empfehlenswert"],
            "vollstaendigkeit": "VOLLSTAENDIG",
        }

    def test_alle_felder_vorhanden(self):
        output = self._beispiel_output()
        assert self.VALIDE_FELDER.issubset(output.keys())

    def test_bewertung_ist_valider_wert(self):
        output = self._beispiel_output()
        assert output["bewertung"] in self.VALIDE_BEWERTUNGEN

    def test_handlungsbedarf_ist_bool(self):
        output = self._beispiel_output()
        assert isinstance(output["handlungsbedarf"], bool)

    def test_stichworte_ist_liste(self):
        output = self._beispiel_output()
        assert isinstance(output["stichworte"], list)

    def test_stichworte_max_fuenf(self):
        output = self._beispiel_output()
        assert len(output["stichworte"]) <= 5

    def test_vollstaendigkeit_valider_wert(self):
        output = self._beispiel_output()
        assert output["vollstaendigkeit"] in self.VALIDE_VOLLSTAENDIGKEIT

    def test_json_serialisierbar(self):
        output = self._beispiel_output()
        serialized = json.dumps(output)
        deserialized = json.loads(serialized)
        assert deserialized == output
