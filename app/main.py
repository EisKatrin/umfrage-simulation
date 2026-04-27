"""Umfrage-Orchestrierungs-Simulation: FastAPI Backend."""

import json
import os
import re
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime

import anthropic
import psycopg2
import psycopg2.extras
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

DATABASE_URL = os.environ["DATABASE_URL"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

BETREFF_ID_RE = re.compile(r"\[([A-Z]{3}-\d{4}-\d{3})\]")


@contextmanager
def db():
    """Öffnet eine DB-Verbindung und schließt sie garantiert nach Benutzung."""
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    cur = conn.cursor()
    try:
        yield conn, cur
    finally:
        cur.close()
        conn.close()


def init_db():
    """Erstellt alle Tabellen falls nicht vorhanden."""
    with db() as (conn, cur):
        cur.execute("""
            CREATE TABLE IF NOT EXISTS umfragen (
                id          SERIAL PRIMARY KEY,
                umfrage_id  VARCHAR(20) UNIQUE NOT NULL,
                titel       TEXT NOT NULL,
                beschreibung TEXT,
                frist       TIMESTAMP NOT NULL,
                status      VARCHAR(20) DEFAULT 'OFFEN',
                erstellt_am TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS teilnehmer (
                id      SERIAL PRIMARY KEY,
                name    TEXT NOT NULL,
                email   VARCHAR(255) UNIQUE NOT NULL,
                region  TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS umfrage_teilnehmer (
                id            SERIAL PRIMARY KEY,
                umfrage_id    INT REFERENCES umfragen(id),
                teilnehmer_id INT REFERENCES teilnehmer(id),
                status        VARCHAR(20) DEFAULT 'AUSSTEHEND',
                versandt_am   TIMESTAMP,
                geantwortet_am TIMESTAMP,
                UNIQUE(umfrage_id, teilnehmer_id)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS antworten (
                id                    SERIAL PRIMARY KEY,
                umfrage_teilnehmer_id INT REFERENCES umfrage_teilnehmer(id),
                roh_text              TEXT,
                extrahierte_daten     JSONB,
                review_status         VARCHAR(20) DEFAULT 'PENDING',
                eingegangen_am        TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS seminar_referenzen (
                id           SERIAL PRIMARY KEY,
                umfrage_id   INT REFERENCES umfragen(id),
                seminar_id   VARCHAR(50) NOT NULL,
                seminar_name TEXT
            )
        """)
        conn.commit()

    if os.environ.get("TESTING") != "1":
        _seed_demo_data()


def _seed_demo_data():
    """Legt Demo-Daten an, falls die DB noch leer ist."""
    with db() as (conn, cur):
        cur.execute("SELECT COUNT(*) FROM umfragen")
        if cur.fetchone()["count"] > 0:
            return

        demo_teilnehmer = [
            ("Anna Müller", "anna.mueller@beispiel.de", "Nord"),
            ("Klaus Schmidt", "k.schmidt@beispiel.de", "Süd"),
            ("Maria Bauer", "m.bauer@beispiel.de", "West"),
        ]
        ids = []
        for name, email, region in demo_teilnehmer:
            cur.execute(
                "INSERT INTO teilnehmer (name, email, region) VALUES (%s, %s, %s) RETURNING id",
                (name, email, region),
            )
            ids.append(cur.fetchone()["id"])

        cur.execute(
            """INSERT INTO umfragen (umfrage_id, titel, beschreibung, frist, status)
               VALUES (%s, %s, %s, %s, %s) RETURNING id""",
            (
                "UMF-2026-001",
                "Seminar-Rückmeldung Q2",
                "Bitte bewerten Sie das Seminar und geben Sie Verbesserungsvorschläge.",
                "2026-05-31 18:00:00",
                "LAUFEND",
            ),
        )
        umfrage_db_id = cur.fetchone()["id"]
        cur.execute(
            "INSERT INTO seminar_referenzen (umfrage_id, seminar_id, seminar_name) VALUES (%s, %s, %s)",
            (umfrage_db_id, "SEM-2026-042", "Führungskräfte-Workshop Nord"),
        )
        for tid in ids:
            cur.execute(
                "INSERT INTO umfrage_teilnehmer (umfrage_id, teilnehmer_id) VALUES (%s, %s)",
                (umfrage_db_id, tid),
            )
        conn.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialisiert die Datenbank beim Start (wird in Tests übersprungen)."""
    if os.environ.get("TESTING") != "1":
        import time
        for attempt in range(10):
            try:
                init_db()
                break
            except Exception:
                if attempt == 9:
                    raise
                time.sleep(2)
    yield


app = FastAPI(title="Umfrage-Simulation", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")


class UmfrageCreate(BaseModel):
    """Eingabe für neue Umfrage."""
    titel: str
    beschreibung: str = ""
    frist: str
    seminar_id: str = ""
    seminar_name: str = ""


class TeilnehmerCreate(BaseModel):
    """Eingabe für neuen Teilnehmer."""
    name: str
    email: str
    region: str = ""


class TeilnehmerHinzufuegen(BaseModel):
    """Teilnehmer-E-Mail zum Hinzufügen zu einer Umfrage."""
    email: str


class SimulatorAntwort(BaseModel):
    """Simulierte eingehende E-Mail."""
    betreff: str
    roh_text: str


def _naechste_umfrage_id(cur) -> str:
    """Generiert die nächste Umfrage-ID im Format UMF-YYYY-NNN."""
    jahr = datetime.now().year
    cur.execute(
        "SELECT umfrage_id FROM umfragen WHERE umfrage_id LIKE %s ORDER BY id DESC LIMIT 1",
        (f"UMF-{jahr}-%",),
    )
    row = cur.fetchone()
    num = (int(row["umfrage_id"].split("-")[-1]) + 1) if row else 1
    return f"UMF-{jahr}-{num:03d}"


def _ki_extraktion(roh_text: str) -> dict:
    """Ruft Claude API auf und extrahiert strukturierte Daten aus dem E-Mail-Text."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    prompt = (
        "Analysiere diese E-Mail-Antwort auf eine Umfrage und extrahiere strukturierte Daten.\n"
        "Gib ausschließlich valides JSON zurück (kein Markdown, keine Erklärung) mit diesen Feldern:\n"
        '- kernaussage: string (1-2 Sätze Zusammenfassung)\n'
        '- bewertung: "POSITIV" | "NEGATIV" | "NEUTRAL" | "UNKLAR"\n'
        '- handlungsbedarf: boolean\n'
        '- stichworte: array mit max. 5 strings\n'
        '- vollstaendigkeit: "VOLLSTAENDIG" | "TEILWEISE" | "UNVOLLSTAENDIG"\n\n'
        f"E-Mail-Text:\n{roh_text}"
    )
    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = message.content[0].text.strip()
    # JSON aus Antwort extrahieren falls Claude Markdown-Fence hinzufügt
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if match:
        raw = match.group(0)
    return json.loads(raw)


@app.get("/")
def dashboard():
    """Liefert das Dashboard als HTML."""
    return FileResponse("static/index.html")


@app.get("/api/stats")
def get_stats():
    """Gibt Kennzahlen für das Dashboard zurück."""
    with db() as (conn, cur):
        cur.execute("""
            SELECT
                (SELECT COUNT(*) FROM umfragen WHERE status != 'ABGESCHLOSSEN') AS offene,
                (SELECT COUNT(*) FROM umfrage_teilnehmer WHERE status = 'AUSSTEHEND') AS ausstehend,
                (SELECT COUNT(*) FROM umfrage_teilnehmer WHERE status = 'ESKALIERT') AS eskaliert,
                (SELECT COUNT(*) FROM antworten WHERE review_status = 'PENDING') AS pending_review
        """)
        row = cur.fetchone()
    return {
        "offene_umfragen": row["offene"],
        "ausstehende_antworten": row["ausstehend"],
        "eskalierte_faelle": row["eskaliert"],
        "pending_review": row["pending_review"],
    }


@app.get("/api/umfragen")
def list_umfragen():
    """Gibt alle Umfragen mit Teilnehmer-Statistik zurück."""
    with db() as (conn, cur):
        cur.execute("""
            SELECT u.*,
                   COALESCE(stats.teilnehmer_gesamt, 0) AS teilnehmer_gesamt,
                   COALESCE(stats.eingegangen, 0)       AS eingegangen,
                   COALESCE(stats.ausstehend, 0)        AS ausstehend,
                   COALESCE(stats.eskaliert, 0)         AS eskaliert,
                   sr.seminar_id, sr.seminar_name
            FROM umfragen u
            LEFT JOIN (
                SELECT umfrage_id,
                       COUNT(*)                                     AS teilnehmer_gesamt,
                       COUNT(*) FILTER (WHERE status = 'EINGEGANGEN') AS eingegangen,
                       COUNT(*) FILTER (WHERE status = 'AUSSTEHEND')  AS ausstehend,
                       COUNT(*) FILTER (WHERE status = 'ESKALIERT')   AS eskaliert
                FROM umfrage_teilnehmer
                GROUP BY umfrage_id
            ) stats ON stats.umfrage_id = u.id
            LEFT JOIN LATERAL (
                SELECT seminar_id, seminar_name
                FROM seminar_referenzen
                WHERE umfrage_id = u.id
                LIMIT 1
            ) sr ON true
            ORDER BY u.erstellt_am DESC
        """)
        rows = cur.fetchall()
    return [dict(r) for r in rows]


@app.get("/api/umfragen/{umfrage_db_id}")
def get_umfrage(umfrage_db_id: int):
    """Gibt Detailansicht einer Umfrage mit allen Teilnehmern und Antworten zurück."""
    with db() as (conn, cur):
        cur.execute("""
            SELECT u.*, sr.seminar_id, sr.seminar_name
            FROM umfragen u
            LEFT JOIN seminar_referenzen sr ON sr.umfrage_id = u.id
            WHERE u.id = %s
        """, (umfrage_db_id,))
        umfrage = cur.fetchone()
        if not umfrage:
            raise HTTPException(404, "Umfrage nicht gefunden")
        cur.execute("""
            SELECT ut.*, t.name, t.email, t.region,
                   a.id AS antwort_id, a.roh_text, a.extrahierte_daten, a.review_status, a.eingegangen_am
            FROM umfrage_teilnehmer ut
            JOIN teilnehmer t ON t.id = ut.teilnehmer_id
            LEFT JOIN antworten a ON a.umfrage_teilnehmer_id = ut.id
            WHERE ut.umfrage_id = %s
            ORDER BY t.name
        """, (umfrage_db_id,))
        teilnehmer = cur.fetchall()
    return {"umfrage": dict(umfrage), "teilnehmer": [dict(t) for t in teilnehmer]}


@app.post("/api/umfragen", status_code=201)
def create_umfrage(data: UmfrageCreate):
    """Legt eine neue Umfrage mit automatisch generierter ID an."""
    with db() as (conn, cur):
        umfrage_id = _naechste_umfrage_id(cur)
        try:
            frist = datetime.fromisoformat(data.frist)
        except ValueError:
            raise HTTPException(422, f"Ungültiges Datumsformat: {data.frist}")
        cur.execute(
            """INSERT INTO umfragen (umfrage_id, titel, beschreibung, frist, status)
               VALUES (%s, %s, %s, %s, 'OFFEN') RETURNING id""",
            (umfrage_id, data.titel, data.beschreibung, frist),
        )
        db_id = cur.fetchone()["id"]
        if data.seminar_id:
            cur.execute(
                "INSERT INTO seminar_referenzen (umfrage_id, seminar_id, seminar_name) VALUES (%s, %s, %s)",
                (db_id, data.seminar_id, data.seminar_name),
            )
        conn.commit()
    return {"id": db_id, "umfrage_id": umfrage_id}


@app.post("/api/teilnehmer", status_code=201)
def create_teilnehmer(data: TeilnehmerCreate):
    """Legt einen neuen Teilnehmer an; gibt bestehende ID zurück bei doppelter E-Mail."""
    with db() as (conn, cur):
        cur.execute(
            """INSERT INTO teilnehmer (name, email, region) VALUES (%s, %s, %s)
               ON CONFLICT (email) DO UPDATE SET name = EXCLUDED.name
               RETURNING id""",
            (data.name, data.email, data.region),
        )
        db_id = cur.fetchone()["id"]
        conn.commit()
    return {"id": db_id}


@app.post("/api/umfragen/{umfrage_db_id}/teilnehmer")
def add_teilnehmer(umfrage_db_id: int, data: TeilnehmerHinzufuegen):
    """Fügt einen Teilnehmer (per E-Mail) zu einer Umfrage hinzu."""
    with db() as (conn, cur):
        cur.execute("SELECT id FROM teilnehmer WHERE email = %s", (data.email,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Teilnehmer mit dieser E-Mail nicht gefunden")
        try:
            cur.execute(
                "INSERT INTO umfrage_teilnehmer (umfrage_id, teilnehmer_id) VALUES (%s, %s)",
                (umfrage_db_id, row["id"]),
            )
            conn.commit()
        except psycopg2.errors.UniqueViolation:
            conn.rollback()
    return {"ok": True}


@app.post("/api/umfragen/{umfrage_db_id}/versenden")
def versenden(umfrage_db_id: int):
    """Simuliert den E-Mail-Versand: setzt alle AUSSTEHEND-Einträge auf VERSANDT."""
    with db() as (conn, cur):
        cur.execute(
            """UPDATE umfrage_teilnehmer SET status = 'VERSANDT', versandt_am = NOW()
               WHERE umfrage_id = %s AND status = 'AUSSTEHEND'""",
            (umfrage_db_id,),
        )
        updated = cur.rowcount
        cur.execute("UPDATE umfragen SET status = 'LAUFEND' WHERE id = %s", (umfrage_db_id,))
        conn.commit()
    return {"versandt_an": updated}


@app.post("/api/simulator/antwort")
def simulator_antwort(data: SimulatorAntwort):
    """Simuliert eine eingehende E-Mail-Antwort und führt KI-Extraktion durch."""
    match = BETREFF_ID_RE.search(data.betreff)
    if not match:
        raise HTTPException(400, "Kein [UMF-YYYY-NNN]-Muster im Betreff gefunden")
    umfrage_id = match.group(1)

    with db() as (conn, cur):
        cur.execute("SELECT id FROM umfragen WHERE umfrage_id = %s", (umfrage_id,))
        umfrage_row = cur.fetchone()
        if not umfrage_row:
            raise HTTPException(404, f"Umfrage {umfrage_id} nicht gefunden")

        cur.execute(
            """SELECT id FROM umfrage_teilnehmer
               WHERE umfrage_id = %s AND status IN ('VERSANDT', 'AUSSTEHEND')
               ORDER BY id LIMIT 1""",
            (umfrage_row["id"],),
        )
        ut_row = cur.fetchone()
        if not ut_row:
            raise HTTPException(400, "Alle Teilnehmer haben bereits geantwortet")

        try:
            extrahiert = _ki_extraktion(data.roh_text)
        except Exception as e:
            extrahiert = {"fehler": str(e), "roh": data.roh_text[:200]}

        cur.execute(
            """INSERT INTO antworten (umfrage_teilnehmer_id, roh_text, extrahierte_daten)
               VALUES (%s, %s, %s) RETURNING id""",
            (ut_row["id"], data.roh_text, json.dumps(extrahiert)),
        )
        antwort_id = cur.fetchone()["id"]
        cur.execute(
            "UPDATE umfrage_teilnehmer SET status = 'EINGEGANGEN', geantwortet_am = NOW() WHERE id = %s",
            (ut_row["id"],),
        )
        conn.commit()

    return {"antwort_id": antwort_id, "umfrage_id": umfrage_id, "extrahiert": extrahiert}


@app.post("/api/antworten/{antwort_id}/freigeben")
def freigeben(antwort_id: int):
    """Human-in-the-Loop: Gibt eine extrahierte Antwort frei."""
    with db() as (conn, cur):
        cur.execute("UPDATE antworten SET review_status = 'FREIGEGEBEN' WHERE id = %s", (antwort_id,))
        conn.commit()
    return {"ok": True}


@app.post("/api/antworten/{antwort_id}/ablehnen")
def ablehnen(antwort_id: int):
    """Human-in-the-Loop: Lehnt eine extrahierte Antwort ab."""
    with db() as (conn, cur):
        cur.execute("UPDATE antworten SET review_status = 'ABGELEHNT' WHERE id = %s", (antwort_id,))
        conn.commit()
    return {"ok": True}


@app.post("/api/umfragen/{umfrage_db_id}/eskalieren")
def eskalieren(umfrage_db_id: int):
    """Setzt alle überfälligen VERSANDT-Teilnehmer auf ESKALIERT."""
    with db() as (conn, cur):
        cur.execute(
            """UPDATE umfrage_teilnehmer ut SET status = 'ESKALIERT'
               FROM umfragen u
               WHERE ut.umfrage_id = u.id
                 AND ut.umfrage_id = %s
                 AND ut.status = 'VERSANDT'
                 AND u.frist < NOW()""",
            (umfrage_db_id,),
        )
        eskaliert_count = cur.rowcount
        conn.commit()
    return {"eskaliert": eskaliert_count}


@app.get("/api/teilnehmer")
def list_teilnehmer():
    """Gibt alle Teilnehmer zurück."""
    with db() as (conn, cur):
        cur.execute("SELECT * FROM teilnehmer ORDER BY name")
        rows = cur.fetchall()
    return [dict(r) for r in rows]
