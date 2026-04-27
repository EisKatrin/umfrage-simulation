"""Integrationstests: API-Endpunkte gegen echte Test-Datenbank."""

import pytest
from unittest.mock import patch


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

class TestStats:
    def test_stats_leer(self, client):
        r = client.get("/api/stats")
        assert r.status_code == 200
        data = r.json()
        assert data["offene_umfragen"] == 0
        assert data["ausstehende_antworten"] == 0
        assert data["eskalierte_faelle"] == 0
        assert data["pending_review"] == 0

    def test_stats_nach_umfrage_anlegen(self, client, demo_umfrage):
        r = client.get("/api/stats")
        assert r.json()["offene_umfragen"] == 1


# ---------------------------------------------------------------------------
# Umfragen CRUD
# ---------------------------------------------------------------------------

class TestUmfragenCRUD:
    def test_umfrage_anlegen(self, client):
        r = client.post("/api/umfragen", json={
            "titel": "Neue Umfrage",
            "frist": "2026-12-31T18:00:00",
        })
        assert r.status_code == 201
        data = r.json()
        assert "umfrage_id" in data
        assert data["umfrage_id"].startswith("UMF-")

    def test_umfrage_id_inkrementiert(self, client):
        r1 = client.post("/api/umfragen", json={"titel": "U1", "frist": "2026-12-31T18:00:00"})
        r2 = client.post("/api/umfragen", json={"titel": "U2", "frist": "2026-12-31T18:00:00"})
        id1 = int(r1.json()["umfrage_id"].split("-")[-1])
        id2 = int(r2.json()["umfrage_id"].split("-")[-1])
        assert id2 == id1 + 1

    def test_umfragen_liste(self, client, demo_umfrage):
        r = client.get("/api/umfragen")
        assert r.status_code == 200
        assert len(r.json()) == 1

    def test_umfrage_detail(self, client, demo_umfrage):
        r = client.get(f"/api/umfragen/{demo_umfrage['id']}")
        assert r.status_code == 200
        data = r.json()
        assert "umfrage" in data
        assert "teilnehmer" in data
        assert data["umfrage"]["titel"] == "Test-Umfrage"

    def test_umfrage_nicht_gefunden(self, client):
        r = client.get("/api/umfragen/99999")
        assert r.status_code == 404

    def test_umfrage_ohne_pflichtfeld_schlaegt_fehl(self, client):
        r = client.post("/api/umfragen", json={"titel": "Kein Datum"})
        assert r.status_code == 422

    def test_seminar_referenz_wird_gespeichert(self, client):
        r = client.post("/api/umfragen", json={
            "titel": "Mit Seminar",
            "frist": "2026-12-31T18:00:00",
            "seminar_id": "SEM-123",
            "seminar_name": "Test Seminar",
        })
        assert r.status_code == 201
        detail = client.get(f"/api/umfragen/{r.json()['id']}")
        umfrage = detail.json()["umfrage"]
        assert umfrage["seminar_id"] == "SEM-123"


# ---------------------------------------------------------------------------
# Teilnehmer
# ---------------------------------------------------------------------------

class TestTeilnehmer:
    def test_teilnehmer_anlegen(self, client):
        r = client.post("/api/teilnehmer", json={
            "name": "Max Mustermann",
            "email": "max@test.de",
            "region": "Ost",
        })
        assert r.status_code == 201
        assert "id" in r.json()

    def test_doppelte_email_liefert_bestehende_id(self, client):
        r1 = client.post("/api/teilnehmer", json={"name": "A", "email": "dup@test.de"})
        r2 = client.post("/api/teilnehmer", json={"name": "B", "email": "dup@test.de"})
        assert r1.json()["id"] == r2.json()["id"]

    def test_teilnehmer_liste(self, client, demo_teilnehmer):
        r = client.get("/api/teilnehmer")
        assert r.status_code == 200
        emails = [t["email"] for t in r.json()]
        assert "test@beispiel.de" in emails

    def test_teilnehmer_zu_umfrage_hinzufuegen(self, client, demo_umfrage, demo_teilnehmer):
        r = client.post(
            f"/api/umfragen/{demo_umfrage['id']}/teilnehmer",
            json={"email": "test@beispiel.de"},
        )
        assert r.status_code == 200

    def test_unbekannte_email_gibt_404(self, client, demo_umfrage):
        r = client.post(
            f"/api/umfragen/{demo_umfrage['id']}/teilnehmer",
            json={"email": "nichtda@test.de"},
        )
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Versenden & Eskalieren
# ---------------------------------------------------------------------------

class TestVersendenEskalieren:
    def test_versenden_aendert_status(self, client, demo_umfrage, demo_teilnehmer):
        client.post(f"/api/umfragen/{demo_umfrage['id']}/teilnehmer", json={"email": "test@beispiel.de"})
        r = client.post(f"/api/umfragen/{demo_umfrage['id']}/versenden")
        assert r.status_code == 200
        assert r.json()["versandt_an"] == 1

    def test_zweifaches_versenden_ohne_effekt(self, client, umfrage_versandt):
        r = client.post(f"/api/umfragen/{umfrage_versandt['id']}/versenden")
        assert r.json()["versandt_an"] == 0

    def test_eskalieren_ohne_ueberfaellige(self, client, umfrage_versandt):
        r = client.post(f"/api/umfragen/{umfrage_versandt['id']}/eskalieren")
        assert r.json()["eskaliert"] == 0


# ---------------------------------------------------------------------------
# Simulator & Antworten
# ---------------------------------------------------------------------------

class TestSimulator:
    MOCK_KI = {
        "kernaussage": "Positives Feedback.",
        "bewertung": "POSITIV",
        "handlungsbedarf": False,
        "stichworte": ["Gut"],
        "vollstaendigkeit": "VOLLSTAENDIG",
    }

    def test_antwort_ohne_id_im_betreff(self, client):
        r = client.post("/api/simulator/antwort", json={
            "betreff": "Kein Muster hier",
            "roh_text": "Text",
        })
        assert r.status_code == 400

    def test_antwort_unbekannte_umfrage_id(self, client):
        r = client.post("/api/simulator/antwort", json={
            "betreff": "[UMF-9999-999] Test",
            "roh_text": "Text",
        })
        assert r.status_code == 404

    def test_antwort_speichert_extraktion(self, client, umfrage_versandt):
        umfrage_id = umfrage_versandt["umfrage_id"]
        with patch("main._ki_extraktion", return_value=self.MOCK_KI):
            r = client.post("/api/simulator/antwort", json={
                "betreff": f"[{umfrage_id}] Re: Test",
                "roh_text": "Das war super!",
            })
        assert r.status_code == 200
        data = r.json()
        assert data["extrahiert"]["bewertung"] == "POSITIV"
        assert "antwort_id" in data

    def test_review_freigeben(self, client, umfrage_versandt):
        umfrage_id = umfrage_versandt["umfrage_id"]
        with patch("main._ki_extraktion", return_value=self.MOCK_KI):
            sim = client.post("/api/simulator/antwort", json={
                "betreff": f"[{umfrage_id}] Re: Test",
                "roh_text": "Super!",
            })
        r = client.post(f"/api/antworten/{sim.json()['antwort_id']}/freigeben")
        assert r.status_code == 200
        assert r.json()["ok"] is True

    def test_review_ablehnen(self, client, umfrage_versandt):
        umfrage_id = umfrage_versandt["umfrage_id"]
        with patch("main._ki_extraktion", return_value=self.MOCK_KI):
            sim = client.post("/api/simulator/antwort", json={
                "betreff": f"[{umfrage_id}] Re: Test",
                "roh_text": "Mäßig.",
            })
        r = client.post(f"/api/antworten/{sim.json()['antwort_id']}/ablehnen")
        assert r.status_code == 200

    def test_stats_nach_antwort_aktualisiert(self, client, umfrage_versandt):
        umfrage_id = umfrage_versandt["umfrage_id"]
        with patch("main._ki_extraktion", return_value=self.MOCK_KI):
            client.post("/api/simulator/antwort", json={
                "betreff": f"[{umfrage_id}] Re: Test",
                "roh_text": "Feedback.",
            })
        stats = client.get("/api/stats").json()
        assert stats["pending_review"] == 1
        assert stats["ausstehende_antworten"] == 0
