"""Pytest-Konfiguration: Test-Datenbank und FastAPI-TestClient."""

import os
import sys
import time
import importlib
import pytest
import psycopg2
import psycopg2.extras
from fastapi.testclient import TestClient

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://umfrage:umfrage2026@localhost:5432/umfrage_test",
)
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ["TESTING"] = "1"
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-placeholder")

for _p in ("/app", str(os.path.join(os.path.dirname(__file__), "..", "app"))):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _get_test_conn():
    """Öffnet eine Verbindung zur Test-Datenbank."""
    return psycopg2.connect(TEST_DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)


def _ensure_test_db():
    """Erstellt die Test-DB falls nötig."""
    base_url = TEST_DATABASE_URL.rsplit("/", 1)[0] + "/postgres"
    conn = psycopg2.connect(base_url)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM pg_database WHERE datname = 'umfrage_test'")
    if not cur.fetchone():
        cur.execute("CREATE DATABASE umfrage_test")
    cur.close()
    conn.close()


def _truncate_all():
    """Leert alle Tabellen (FK-sicher).

    FastAPI führt sync-Routen in einem Thread-Pool aus: TestClient bekommt
    die Antwort zurück, bevor conn.close() im Route-Thread abgeschlossen ist.
    Deshalb: zuerst alle offenen Verbindungen beenden und kurz warten, dann
    TRUNCATE mit lock_timeout ausführen.
    """
    admin_url = TEST_DATABASE_URL.rsplit("/", 1)[0] + "/postgres"
    admin = psycopg2.connect(admin_url)
    admin.autocommit = True
    admin_cur = admin.cursor()

    # Wiederholt terminieren + warten bis keine Verbindungen mehr offen sind
    for _ in range(10):
        admin_cur.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = 'umfrage_test' AND pid <> pg_backend_pid()"
        )
        admin_cur.execute(
            "SELECT COUNT(*) FROM pg_stat_activity WHERE datname = 'umfrage_test'"
        )
        remaining = admin_cur.fetchone()[0]
        if remaining == 0:
            break
        time.sleep(0.1)

    admin_cur.close()
    admin.close()

    conn = _get_test_conn()
    cur = conn.cursor()
    cur.execute("SET lock_timeout = '10s'")
    cur.execute("""
        TRUNCATE antworten, seminar_referenzen, umfrage_teilnehmer,
                 teilnehmer, umfragen RESTART IDENTITY CASCADE
    """)
    conn.commit()
    cur.close()
    conn.close()


@pytest.fixture(scope="session")
def setup_test_db():
    """Einmaliges Session-Setup: Test-DB + Schema anlegen."""
    _ensure_test_db()
    main_mod = importlib.import_module("main")
    main_mod.init_db()
    yield


@pytest.fixture()
def clean_tables(setup_test_db):
    """Leert Tabellen VOR jedem DB-Test."""
    _truncate_all()
    yield


@pytest.fixture()
def client(clean_tables):
    """FastAPI-TestClient pro Test."""
    main_mod = importlib.import_module("main")
    yield TestClient(main_mod.app, raise_server_exceptions=True)


@pytest.fixture()
def demo_umfrage(client):
    """Legt eine Test-Umfrage an."""
    r = client.post("/api/umfragen", json={
        "titel": "Test-Umfrage",
        "beschreibung": "Automatisierter Testlauf",
        "frist": "2026-12-31T18:00:00",
        "seminar_id": "SEM-TEST-001",
        "seminar_name": "Test-Seminar",
    })
    assert r.status_code == 201
    return r.json()


@pytest.fixture()
def demo_teilnehmer(client):
    """Legt einen Test-Teilnehmer an."""
    r = client.post("/api/teilnehmer", json={
        "name": "Test Person",
        "email": "test@beispiel.de",
        "region": "Nord",
    })
    assert r.status_code == 201
    return r.json()


@pytest.fixture()
def umfrage_versandt(client, demo_umfrage, demo_teilnehmer):
    """Umfrage mit einem zugeordneten, bereits versandten Teilnehmer."""
    client.post(
        f"/api/umfragen/{demo_umfrage['id']}/teilnehmer",
        json={"email": "test@beispiel.de"},
    )
    client.post(f"/api/umfragen/{demo_umfrage['id']}/versenden")
    return demo_umfrage
