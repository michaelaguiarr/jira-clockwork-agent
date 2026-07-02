"""
Jira Clockwork Agent
Lê eventos do Google Calendar e lança worklogs no Jira (sincronizado com Clockwork Pro).
Roda via GitHub Actions duas vezes ao dia.
"""

import os
import re
import json
import base64
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

# ── Configuração de log ────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Constantes ─────────────────────────────────────────────────────────────────
SCG_PATTERN = re.compile(r"\b(SCG-\d+)\b", re.IGNORECASE)
LOGGED_FILE  = Path("logged_worklogs.json")   # controle de duplicatas (comitado no repo)
BRT          = timezone(timedelta(hours=-3))


# ── Helpers Google Calendar ────────────────────────────────────────────────────

def build_calendar_service():
    """Constrói o serviço do Google Calendar usando credenciais OAuth2."""
    creds_json = os.environ["GOOGLE_CREDENTIALS_JSON"]   # JSON do token OAuth2
    token_data  = json.loads(creds_json)

    creds = Credentials(
        token=token_data.get("token"),
        refresh_token=token_data["refresh_token"],
        token_uri=token_data.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=token_data["client_id"],
        client_secret=token_data["client_secret"],
        scopes=["https://www.googleapis.com/auth/calendar.readonly"],
    )

    # Renova o access token se expirado
    if creds.expired or not creds.token:
        creds.refresh(Request())

    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def get_todays_events(service) -> list[dict]:
    """Retorna todos os eventos de hoje (meia-noite até agora em BRT)."""
    now_brt   = datetime.now(BRT)
    start_brt = now_brt.replace(hour=0, minute=0, second=0, microsecond=0)

    time_min = start_brt.isoformat()
    time_max = now_brt.isoformat()

    log.info("Buscando eventos de %s até %s", time_min, time_max)

    result = service.events().list(
        calendarId="primary",
        timeMin=time_min,
        timeMax=time_max,
        singleEvents=True,
        orderBy="startTime",
    ).execute()

    return result.get("items", [])


def parse_event(event: dict) -> dict | None:
    """
    Extrai SCG-key, duração e título de um evento.
    Retorna None se o evento não tiver SCG no título.
    """
    title = event.get("summary", "")
    match = SCG_PATTERN.search(title)
    if not match:
        return None

    issue_key = match.group(1).upper()

    # Eventos com data/hora (não dia-inteiro)
    start_raw = event.get("start", {})
    end_raw   = event.get("end", {})

    if "dateTime" not in start_raw:
        log.debug("Evento '%s' ignorado: é dia-inteiro.", title)
        return None

    start_dt = datetime.fromisoformat(start_raw["dateTime"])
    end_dt   = datetime.fromisoformat(end_raw["dateTime"])
    duration_seconds = int((end_dt - start_dt).total_seconds())

    if duration_seconds <= 0:
        return None

    return {
        "event_id":        event["id"],
        "issue_key":       issue_key,
        "title":           title,
        "start":           start_dt.isoformat(),
        "duration_seconds": duration_seconds,
        "started_jira":    start_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000+0000"),
    }


# ── Helpers Jira ───────────────────────────────────────────────────────────────

def jira_auth_header() -> str:
    email = os.environ["JIRA_EMAIL"]
    token = os.environ["JIRA_API_TOKEN"]
    return "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode()


def log_worklog(domain: str, parsed: dict) -> bool:
    """Lança worklog no Jira. Retorna True se sucesso."""
    url = f"https://{domain}/rest/api/3/issue/{parsed['issue_key']}/worklog"

    # Monta comentário com título do evento (removendo o prefixo SCG-XXXX -)
    comment_text = re.sub(r"^SCG-\d+\s*[-–]\s*", "", parsed["title"]).strip()

    body = {
        "timeSpentSeconds": parsed["duration_seconds"],
        "started": parsed["started_jira"],
        "comment": {
            "type": "doc",
            "version": 1,
            "content": [{
                "type": "paragraph",
                "content": [{"type": "text", "text": comment_text or parsed["title"]}],
            }],
        },
    }

    resp = requests.post(
        url,
        headers={
            "Authorization": jira_auth_header(),
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        json=body,
        timeout=15,
    )

    if resp.status_code in (200, 201):
        log.info("✅ Worklog lançado: %s | %ds | '%s'",
                 parsed["issue_key"], parsed["duration_seconds"], comment_text)
        return True
    else:
        log.error("❌ Falha ao lançar worklog em %s: %s %s",
                  parsed["issue_key"], resp.status_code, resp.text)
        return False


# ── Controle de duplicatas ─────────────────────────────────────────────────────

def load_logged() -> set[str]:
    if LOGGED_FILE.exists():
        data = json.loads(LOGGED_FILE.read_text())
        return set(data.get("logged_event_ids", []))
    return set()


def save_logged(logged: set[str]):
    LOGGED_FILE.write_text(json.dumps({"logged_event_ids": sorted(logged)}, indent=2))


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    domain = os.environ["JIRA_DOMAIN"]   # ex: minha-empresa.atlassian.net

    log.info("=== Jira Clockwork Agent iniciado ===")

    # 1. Buscar eventos do Calendar
    service = build_calendar_service()
    events  = get_todays_events(service)
    log.info("%d evento(s) encontrado(s) hoje.", len(events))

    # 2. Filtrar os que têm SCG
    parsed_events = [p for e in events if (p := parse_event(e)) is not None]
    log.info("%d evento(s) com SCG no título.", len(parsed_events))

    if not parsed_events:
        log.info("Nada a lançar. Encerrando.")
        return

    # 3. Carregar controle de duplicatas
    already_logged = load_logged()
    new_logged     = set(already_logged)

    # 4. Lançar worklogs ainda não registrados
    launched = 0
    for parsed in parsed_events:
        eid = parsed["event_id"]
        if eid in already_logged:
            log.info("⏭️  Já lançado anteriormente: %s (%s)", parsed["issue_key"], eid[:8])
            continue

        success = log_worklog(domain, parsed)
        if success:
            new_logged.add(eid)
            launched += 1

    # 5. Persistir controle
    save_logged(new_logged)

    log.info("=== Concluído: %d worklog(s) lançado(s) ===", launched)


if __name__ == "__main__":
    main()
