"""CI/SecCD Security-Tests: OWASP-relevante Angriffsvektoren."""

import pytest
from unittest.mock import patch


# ---------------------------------------------------------------------------
# SQL-Injection
# ---------------------------------------------------------------------------

class TestSQLInjection:
    """Prüft dass alle Eingaben parameterisiert verarbeitet werden."""

    PAYLOADS = [
        "'; DROP TABLE umfragen; --",
        "1 OR 1=1",
        "' UNION SELECT * FROM teilnehmer --",
        "admin'--",
        "1; SELECT pg_sleep(5)--",
    ]

    def test_sql_injection_im_titel(self, client):
        for payload in self.PAYLOADS:
            r = client.post("/api/umfragen", json={
                "titel": payload,
                "frist": "2026-12-31T18:00:00",
            })
            # Muss entweder 201 (gespeichert als Text) oder 422 (Validierung) zurückgeben
            # Niemals 500 (Datenbankfehler durch Injection)
            assert r.status_code in (201, 422), \
                f"Payload '{payload}' führte zu Status {r.status_code}"

    def test_sql_injection_in_email(self, client):
        for payload in self.PAYLOADS:
            r = client.post("/api/teilnehmer", json={
                "name": "Test",
                "email": f"test+{payload}@test.de",
            })
            assert r.status_code in (201, 422), \
                f"Payload '{payload}' führte zu Status {r.status_code}"

    def test_sql_injection_im_betreff(self, client):
        payloads_in_betreff = [
            "[UMF-2026-001] '; DROP TABLE umfragen; --",
            "[UMF-2026-001]' OR '1'='1",
        ]
        for payload in payloads_in_betreff:
            r = client.post("/api/simulator/antwort", json={
                "betreff": payload,
                "roh_text": "Test",
            })
            # 400 (keine valide ID) oder 404 (Umfrage nicht gefunden) – niemals 500
            assert r.status_code in (400, 404, 422), \
                f"Payload führte zu Status {r.status_code}"

    def test_datenbank_intakt_nach_injection_versuchen(self, client):
        """Tabellen müssen nach allen Injection-Versuchen noch existieren."""
        r = client.get("/api/stats")
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Input-Validierung & Größenbeschränkung
# ---------------------------------------------------------------------------

class TestInputValidierung:
    """Prüft dass überlange oder fehlerhafte Eingaben abgewiesen werden."""

    def test_fehlende_pflichtfelder_umfrage(self, client):
        r = client.post("/api/umfragen", json={})
        assert r.status_code == 422

    def test_fehlende_pflichtfelder_teilnehmer(self, client):
        r = client.post("/api/teilnehmer", json={"name": "Nur Name"})
        assert r.status_code == 422

    def test_invalides_datum_format(self, client):
        r = client.post("/api/umfragen", json={
            "titel": "Test",
            "frist": "kein-datum",
        })
        assert r.status_code == 422

    def test_leerer_betreff_im_simulator(self, client):
        r = client.post("/api/simulator/antwort", json={
            "betreff": "",
            "roh_text": "Text",
        })
        assert r.status_code == 400

    def test_sehr_langer_titel(self, client):
        """Extrem langer Input darf keinen 500er verursachen."""
        r = client.post("/api/umfragen", json={
            "titel": "A" * 100_000,
            "frist": "2026-12-31T18:00:00",
        })
        assert r.status_code in (201, 422, 400), \
            f"Langer Titel führte zu Status {r.status_code}"

    def test_nicht_existierende_umfrage_id(self, client):
        r = client.get("/api/umfragen/99999")
        assert r.status_code == 404

    def test_negativer_pfad_parameter(self, client):
        r = client.get("/api/umfragen/-1")
        assert r.status_code in (404, 422)

    def test_string_statt_int_pfad_parameter(self, client):
        r = client.get("/api/umfragen/abc")
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# HTTP-Methoden-Beschränkung
# ---------------------------------------------------------------------------

class TestHttpMethoden:
    """Nicht erlaubte HTTP-Methoden müssen abgewiesen werden."""

    def test_delete_auf_umfragen_liste(self, client):
        r = client.delete("/api/umfragen")
        assert r.status_code == 405

    def test_put_auf_stats(self, client):
        r = client.put("/api/stats")
        assert r.status_code == 405

    def test_patch_auf_simulator(self, client):
        r = client.patch("/api/simulator/antwort")
        assert r.status_code == 405


# ---------------------------------------------------------------------------
# Sensitive Daten im Response
# ---------------------------------------------------------------------------

class TestSensitiveDaten:
    """Prüft dass keine internen Fehlermeldungen oder Pfade nach außen gelangen."""

    def test_kein_stack_trace_bei_fehler(self, client):
        r = client.get("/api/umfragen/99999")
        body = r.text.lower()
        assert "traceback" not in body
        assert "psycopg2" not in body
        assert "/app/" not in body

    def test_kein_datenbankfehler_im_response(self, client):
        r = client.post("/api/teilnehmer", json={
            "name": "Test",
            "email": "test@beispiel.de",
        })
        if r.status_code != 201:
            body = r.text.lower()
            assert "postgresql" not in body
            assert "umfrage2026" not in body  # kein Passwort im Response

    def test_content_type_ist_json(self, client):
        r = client.get("/api/stats")
        assert "application/json" in r.headers.get("content-type", "")


# ---------------------------------------------------------------------------
# Rate Limiting / Replay-Schutz (konzeptionell)
# ---------------------------------------------------------------------------

class TestIdempotenz:
    """Prüft sichere Mehrfach-Aufrufe ohne ungewollte Seiteneffekte."""

    def test_doppelter_teilnehmer_gibt_selbe_id(self, client):
        payload = {"name": "Doppelt", "email": "doppelt@test.de"}
        r1 = client.post("/api/teilnehmer", json=payload)
        r2 = client.post("/api/teilnehmer", json=payload)
        assert r1.json()["id"] == r2.json()["id"]

    def test_mehrfaches_versenden_eskaliert_nicht(self, client):
        """Zweifaches Versenden darf keine Antworten doppelt anlegen."""
        r = client.post("/api/umfragen", json={
            "titel": "Idempotenz-Test",
            "frist": "2026-12-31T18:00:00",
        })
        uid = r.json()["id"]
        client.post(f"/api/umfragen/{uid}/versenden")
        client.post(f"/api/umfragen/{uid}/versenden")
        stats = client.get("/api/stats").json()
        # Keine doppelten Einträge → ausstehend bleibt 0 (keine Teilnehmer)
        assert stats["ausstehende_antworten"] == 0
