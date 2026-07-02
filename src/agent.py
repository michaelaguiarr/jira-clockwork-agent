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
TOLERANCE_S  = 300   # 5 min de tolerância ao comparar duração com worklogs existentes


# ── Helpers Google Calendar ────────────────────────────────────────────────────

def build_calendar_service():
    """Constrói o serviço do Google Calendar usando credenciais OAuth2."""
    creds_json = os.environ["GOOGLE_CREDENTIALS_JSON"]
    token_data  = json.loads(creds_json)

    creds = Credentials(
        token=token_data.get("token"),
        refresh_token=token_data["refresh_token"],
        token_uri=token_data.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=token_data["client_id"],
        client_secret=token_data["client_secret"],
        scopes=["https://www.googleapis.com/auth/calendar.readonly"],
    )

    if creds.expired or not creds.token:
        creds.refresh(Request())

    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def get_recent_events(service) -> list[dict]:
    """
    Retorna eventos entre START_DATE (ou LOOKBACK_DAYS atrás) e agora.
    START_DATE tem prioridade sobre LOOKBACK_DAYS quando definida.
    """
    now_brt = datetime.now(BRT)

    start_date_env = os.environ.get("START_DATE", "").strip()
    if start_date_env:
        # Formato esperado: YYYY-MM-DD
        start_brt = datetime.strptime(start_date_env, "%Y-%m-%d").replace(
            tzinfo=BRT, hour=0, minute=0, second=0
        )
        log.info("Data de corte (START_DATE): %s", start_date_env)
    else:
        lookback  = int(os.environ.get("LOOKBACK_DAYS", "7"))
        start_brt = (now_brt - timedelta(days=lookback)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        log.info("Janela de busca: últimos %d dia(s)", lookback)

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
    Retorna None se o evento não tiver SCG no título ou for dia-inteiro.
    """
    title = event.get("summary", "")
    match = SCG_PATTERN.search(title)
    if not match:
        return None

    issue_key = match.group(1).upper()

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
        "event_id":         event["id"],
        "issue_key":        issue_key,
        "title":            title,
        "start":            start_dt.isoformat(),
        "duration_seconds": duration_seconds,
        "started_jira":     start_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000+0000"),
        "date":             start_dt.astimezone(BRT).date().isoformat(),
    }


# ── Helpers Jira ───────────────────────────────────────────────────────────────

def jira_auth_header() -> str:
    email = os.environ["JIRA_EMAIL"]
    token = os.environ["JIRA_API_TOKEN"]
    return "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode()


def already_logged_in_jira(domain: str, issue_key: str, date: str, duration_seconds: int) -> bool:
    """
    Verifica se já existe worklog no Jira para o ticket naquele dia com duração parecida.
    Usa tolerância de TOLERANCE_S segundos para comparar duração.
    """
    url = f"https://{domain}/rest/api/3/issue/{issue_key}/worklog"
    try:
        resp = requests.get(
            url,
            headers={
                "Authorization": jira_auth_header(),
                "Accept": "application/json",
            },
            timeout=15,
        )
        if resp.status_code != 200:
            log.warning("Não conseguiu verificar worklogs existentes em %s: %s", issue_key, resp.status_code)
            return False

        worklogs = resp.json().get("worklogs", [])
        for wl in worklogs:
            # Compara data (dia) e duração com tolerância
            started_raw = wl.get("started", "")
            wl_date = started_raw[:10]   # YYYY-MM-DD
            wl_duration = wl.get("timeSpentSeconds", 0)

            if wl_date == date and abs(wl_duration - duration_seconds) <= TOLERANCE_S:
                log.info("⚠️  Worklog já existe no Jira: %s | %s | %ds", issue_key, wl_date, wl_duration)
                return True

    except Exception as e:
        log.warning("Erro ao verificar worklogs existentes em %s: %s", issue_key, e)

    return False


def log_worklog(domain: str, parsed: dict) -> bool:
    """Lança worklog no Jira. Retorna True se sucesso."""
    url = f"https://{domain}/rest/api/3/issue/{parsed['issue_key']}/worklog"

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


# ── Controle de duplicatas (local) ─────────────────────────────────────────────

def load_logged() -> set[str]:
    if LOGGED_FILE.exists():
        data = json.loads(LOGGED_FILE.read_text())
        return set(data.get("logged_event_ids", []))
    return set()


def save_logged(logged: set[str]):
    LOGGED_FILE.write_text(json.dumps({"logged_event_ids": sorted(logged)}, indent=2))


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    domain = os.environ["JIRA_DOMAIN"]

    log.info("=== Jira Clockwork Agent iniciado ===")

    # 1. Buscar eventos do Calendar
    service = build_calendar_service()
    events  = get_recent_events(service)
    log.info("%d evento(s) encontrado(s) no período.", len(events))

    # 2. Filtrar os que têm SCG
    parsed_events = [p for e in events if (p := parse_event(e)) is not None]
    log.info("%d evento(s) com SCG no título.", len(parsed_events))

    if not parsed_events:
        log.info("Nada a lançar. Encerrando.")
        return

    # 3. Carregar controle local de duplicatas
    already_logged = load_logged()
    new_logged     = set(already_logged)

    # 4. Processar cada evento
    launched = 0
    for parsed in parsed_events:
        eid = parsed["event_id"]

        # 4a. Já lançado por este agente anteriormente?
        if eid in already_logged:
            log.info("⏭️  Já lançado pelo agente: %s (%s)", parsed["issue_key"], eid[:8])
            continue

        # 4b. Já existe worklog manual no Jira para este ticket/dia/duração?
        if already_logged_in_jira(domain, parsed["issue_key"], parsed["date"], parsed["duration_seconds"]):
            log.info("⏭️  Ignorado (worklog manual detectado): %s | %s", parsed["issue_key"], parsed["date"])
            new_logged.add(eid)   # marca para não verificar de novo
            continue

        # 4c. Lança o worklog
        success = log_worklog(domain, parsed)
        if success:
            new_logged.add(eid)
            launched += 1

    # 5. Persistir controle local
    save_logged(new_logged)

    log.info("=== Concluído: %d worklog(s) lançado(s) ===", launched)


if __name__ == "__main__":
    main()