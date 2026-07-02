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
LOGGED_FILE  = Path("logged_worklogs.json")
BRT          = timezone(timedelta(hours=-3))
TOLERANCE_S  = 300   # 5 min de tolerância ao comparar duração com worklogs existentes


# ── Telegram ───────────────────────────────────────────────────────────────────

def send_telegram(text: str):
    """Envia mensagem via Telegram. Falha silenciosa para não quebrar o agente."""
    token   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        log.warning("Telegram não configurado — TELEGRAM_BOT_TOKEN ou TELEGRAM_CHAT_ID ausente.")
        return
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        if not resp.ok:
            log.warning("Telegram: falha ao enviar mensagem: %s", resp.text)
    except Exception as e:
        log.warning("Telegram: erro de conexão: %s", e)


def format_seconds(seconds: int) -> str:
    """Converte segundos em formato legível (ex: 1h30m)."""
    h = seconds // 3600
    m = (seconds % 3600) // 60
    if h and m:
        return f"{h}h{m:02d}m"
    if h:
        return f"{h}h"
    return f"{m}m"


def notify_daily(launched: list[dict], skipped: list[dict], errors: list[dict]):
    """Envia notificação diária com resumo da execução."""
    now_brt = datetime.now(BRT).strftime("%d/%m/%Y %H:%M")
    lines   = [f"⏱ <b>Clockwork Agent</b> — {now_brt}\n"]

    if launched:
        lines.append("✅ <b>Lançados:</b>")
        for w in launched:
            lines.append(f"  • {w['issue_key']}  |  {format_seconds(w['duration_seconds'])}  |  {w['comment']}")

    if skipped:
        lines.append("\n⏭ <b>Já existiam no Jira:</b>")
        for w in skipped:
            lines.append(f"  • {w['issue_key']}  |  {w['date']}")

    if errors:
        lines.append("\n❌ <b>Erros:</b>")
        for w in errors:
            lines.append(f"  • {w['issue_key']}  —  {w['reason']}")

    if not launched and not skipped and not errors:
        lines.append("📭 Nenhum evento com SCG encontrado no período.")

    total_s = sum(w["duration_seconds"] for w in launched)
    if launched:
        lines.append(f"\n⏱ <b>Total lançado:</b> {format_seconds(total_s)}")

    send_telegram("\n".join(lines))


def notify_weekly(weekly_logs: list[dict]):
    """Envia relatório semanal toda sexta-feira na execução das 18h."""
    now_brt   = datetime.now(BRT)
    start_week = (now_brt - timedelta(days=4)).strftime("%d/%m")
    end_week   = now_brt.strftime("%d/%m")

    lines = [f"📊 <b>Resumo semanal</b> — {start_week} a {end_week}\n"]

    if not weekly_logs:
        lines.append("📭 Nenhum worklog lançado esta semana.")
    else:
        days_with_log = set()
        total_s = 0
        for w in weekly_logs:
            lines.append(f"✅ {w['issue_key']}  |  {format_seconds(w['duration_seconds'])}  |  {w['comment']}")
            days_with_log.add(w["date"])
            total_s += w["duration_seconds"]

        lines.append(f"\n⏱ <b>Total semana:</b> {format_seconds(total_s)}")
        lines.append(f"📅 <b>Dias com lançamento:</b> {len(days_with_log)} de 5")

    send_telegram("\n".join(lines))


# ── Helpers Google Calendar ────────────────────────────────────────────────────

def build_calendar_service():
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
    """Retorna eventos entre START_DATE (ou LOOKBACK_DAYS atrás) e agora."""
    now_brt = datetime.now(BRT)

    start_date_env = os.environ.get("START_DATE", "").strip()
    if start_date_env:
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


def get_week_events(service) -> list[dict]:
    """Retorna eventos SCG da semana atual (seg a hoje) para o relatório semanal."""
    now_brt   = datetime.now(BRT)
    monday    = (now_brt - timedelta(days=now_brt.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    time_min  = monday.isoformat()
    time_max  = now_brt.isoformat()

    result = service.events().list(
        calendarId="primary",
        timeMin=time_min,
        timeMax=time_max,
        singleEvents=True,
        orderBy="startTime",
    ).execute()

    return result.get("items", [])


def parse_event(event: dict) -> dict | None:
    title = event.get("summary", "")
    match = SCG_PATTERN.search(title)
    if not match:
        return None

    issue_key = match.group(1).upper()
    start_raw = event.get("start", {})
    end_raw   = event.get("end", {})

    if "dateTime" not in start_raw:
        return None

    start_dt         = datetime.fromisoformat(start_raw["dateTime"])
    end_dt           = datetime.fromisoformat(end_raw["dateTime"])
    duration_seconds = int((end_dt - start_dt).total_seconds())

    if duration_seconds <= 0:
        return None

    comment = re.sub(r"^SCG-\d+\s*[-–]\s*", "", title).strip() or title

    return {
        "event_id":         event["id"],
        "issue_key":        issue_key,
        "title":            title,
        "comment":          comment,
        "start":            start_dt.isoformat(),
        "date":             start_dt.astimezone(BRT).date().isoformat(),
        "duration_seconds": duration_seconds,
        "started_jira":     start_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000+0000"),
    }


# ── Helpers Jira ───────────────────────────────────────────────────────────────

def jira_auth_header() -> str:
    email = os.environ["JIRA_EMAIL"]
    token = os.environ["JIRA_API_TOKEN"]
    return "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode()


def already_logged_in_jira(domain: str, issue_key: str, date: str, duration_seconds: int) -> bool:
    url = f"https://{domain}/rest/api/3/issue/{issue_key}/worklog"
    try:
        resp = requests.get(
            url,
            headers={"Authorization": jira_auth_header(), "Accept": "application/json"},
            timeout=15,
        )
        if resp.status_code != 200:
            return False

        for wl in resp.json().get("worklogs", []):
            wl_date     = wl.get("started", "")[:10]
            wl_duration = wl.get("timeSpentSeconds", 0)
            if wl_date == date and abs(wl_duration - duration_seconds) <= TOLERANCE_S:
                log.info("⚠️  Worklog já existe no Jira: %s | %s | %ds", issue_key, wl_date, wl_duration)
                return True
    except Exception as e:
        log.warning("Erro ao verificar worklogs existentes em %s: %s", issue_key, e)
    return False


def log_worklog(domain: str, parsed: dict) -> bool:
    url  = f"https://{domain}/rest/api/3/issue/{parsed['issue_key']}/worklog"
    body = {
        "timeSpentSeconds": parsed["duration_seconds"],
        "started": parsed["started_jira"],
        "comment": {
            "type": "doc",
            "version": 1,
            "content": [{
                "type": "paragraph",
                "content": [{"type": "text", "text": parsed["comment"]}],
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
                 parsed["issue_key"], parsed["duration_seconds"], parsed["comment"])
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
    domain  = os.environ["JIRA_DOMAIN"]
    now_brt = datetime.now(BRT)

    log.info("=== Jira Clockwork Agent iniciado ===")

    service = build_calendar_service()
    events  = get_recent_events(service)
    log.info("%d evento(s) encontrado(s) no período.", len(events))

    parsed_events = [p for e in events if (p := parse_event(e)) is not None]
    log.info("%d evento(s) com SCG no título.", len(parsed_events))

    already_logged = load_logged()
    new_logged     = set(already_logged)

    launched = []
    skipped  = []
    errors   = []

    for parsed in parsed_events:
        eid = parsed["event_id"]

        if eid in already_logged:
            # Já processado anteriormente — não notifica, só ignora
            log.info("⏭️  Já lançado pelo agente: %s (%s)", parsed["issue_key"], eid[:8])
            continue

        if already_logged_in_jira(domain, parsed["issue_key"], parsed["date"], parsed["duration_seconds"]):
            # Novo para o agente mas já existe no Jira (lançamento manual) — notifica
            log.info("⏭️  Worklog manual detectado: %s | %s", parsed["issue_key"], parsed["date"])
            skipped.append(parsed)
            new_logged.add(eid)
            continue

        success = log_worklog(domain, parsed)
        if success:
            new_logged.add(eid)
            launched.append(parsed)
        else:
            errors.append({**parsed, "reason": "Falha na API do Jira"})

    save_logged(new_logged)

    # Notificação diária
    notify_daily(launched, skipped, errors)

    # Relatório semanal — toda sexta-feira na execução das 18h BRT
    is_friday   = now_brt.weekday() == 4
    is_evening  = now_brt.hour >= 18
    if is_friday and is_evening:
        log.info("Sexta-feira à noite — gerando relatório semanal...")
        week_events  = get_week_events(service)
        weekly_logs  = [p for e in week_events if (p := parse_event(e)) is not None
                        and e["id"] in new_logged]
        notify_weekly(weekly_logs)

    log.info("=== Concluído: %d worklog(s) lançado(s) ===", len(launched))


if __name__ == "__main__":
    main()