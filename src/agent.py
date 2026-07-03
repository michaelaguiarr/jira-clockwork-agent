"""
Jira Clockwork Agent
Lê eventos do Google Calendar e lança worklogs no Jira (sincronizado com Clockwork Pro).
Roda via GitHub Actions a cada 30 minutos — o agente decide o que fazer baseado na hora BRT:
  - 18h00–18h59 BRT: lembrete para lançar eventos no Calendar
  - 23h30–00h29 BRT: lança worklogs + verifica horas faltantes + relatório semanal (sextas)
  - Outros horários: encerra silenciosamente
"""

import os
import re
import json
import base64
import logging
import time
from datetime import datetime, timezone, timedelta
import calendar
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
TICKET_PATTERN = re.compile(r"\b([A-Z]+-\d+)\b")
LOGGED_FILE    = Path("logged_worklogs.json")
HEALTH_FILE    = Path("health.json")
BRT            = timezone(timedelta(hours=-3))
TOLERANCE_S    = 300
DAILY_GOAL_S   = int(os.environ.get("DAILY_HOURS_GOAL", "8")) * 3600
MAX_RETRIES    = 3
RETRY_DELAYS   = [5, 10, 20]

# ── Janelas de execução BRT ────────────────────────────────────────────────────
# Lembrete:   18h00–18h59
# Lançamento: 23h30–23h59 ou 00h00–00h29 (passa da meia-noite)
REMINDER_START = (18, 0)
REMINDER_END   = (18, 59)
LAUNCH_START   = (23, 30)
LAUNCH_END     = (0, 29)


def get_execution_mode(now_brt: datetime, force_mode: str) -> str:
    """
    Retorna o modo de execução baseado na hora BRT atual.
    Modos: 'reminder', 'launch', 'skip'
    """
    if force_mode == "reminder":
        return "reminder"
    if force_mode == "launch":
        return "launch"

    h, m  = now_brt.hour, now_brt.minute
    total = h * 60 + m

    reminder_start = REMINDER_START[0] * 60 + REMINDER_START[1]
    reminder_end   = REMINDER_END[0]   * 60 + REMINDER_END[1]
    launch_start   = LAUNCH_START[0]   * 60 + LAUNCH_START[1]
    launch_end     = LAUNCH_END[0]     * 60 + LAUNCH_END[1]

    if reminder_start <= total <= reminder_end:
        return "reminder"

    # Lançamento: 23h30–23h59 ou 00h00–00h29 (passa da meia-noite)
    if total >= launch_start or total <= launch_end:
        return "launch"

    return "skip"


# ── Retry automático ───────────────────────────────────────────────────────────

def requests_with_retry(method: str, url: str, max_retries: int = MAX_RETRIES, **kwargs) -> requests.Response:
    for attempt in range(max_retries):
        try:
            resp = requests.request(method, url, **kwargs)

            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", RETRY_DELAYS[attempt]))
                log.warning("Rate limit (429) — aguardando %ds (tentativa %d/%d)", wait, attempt + 1, max_retries)
                time.sleep(wait)
                continue

            if resp.status_code >= 500:
                if attempt < max_retries - 1:
                    delay = RETRY_DELAYS[attempt]
                    log.warning("Erro %s — aguardando %ds (tentativa %d/%d)", resp.status_code, delay, attempt + 1, max_retries)
                    time.sleep(delay)
                    continue

            return resp

        except (requests.ConnectionError, requests.Timeout) as e:
            if attempt < max_retries - 1:
                delay = RETRY_DELAYS[attempt]
                log.warning("Erro de conexão: %s — aguardando %ds (tentativa %d/%d)", e, delay, attempt + 1, max_retries)
                time.sleep(delay)
            else:
                log.error("Todas as %d tentativas falharam: %s", max_retries, e)
                raise

    return resp


# ── Health check ───────────────────────────────────────────────────────────────

def save_health(status: str, launched: int = 0, error: str = ""):
    data = {
        "last_run":          datetime.now(BRT).strftime("%Y-%m-%dT%H:%M:%S"),
        "status":            status,
        "worklogs_launched": launched,
        "error":             error,
    }
    HEALTH_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    log.info("Health check salvo: status=%s worklogs=%d", status, launched)


# ── Verificação preventiva de tokens ──────────────────────────────────────────

def check_jira_token(domain: str) -> bool:
    url = f"https://{domain}/rest/api/3/myself"
    try:
        resp = requests.get(
            url,
            headers={"Authorization": jira_auth_header(), "Accept": "application/json"},
            timeout=10,
        )
        if resp.status_code == 200:
            display_name = resp.json().get("displayName", "")
            log.info("✅ Token Jira válido — usuário: %s", display_name)
            return True
        elif resp.status_code == 401:
            log.error("❌ Token Jira inválido ou expirado (401)")
            send_telegram_alert(
                "🔑 <b>Clockwork Agent — Token Jira Inválido</b>\n\n"
                "O token de API do Jira está inválido ou foi revogado.\n\n"
                "Gere um novo em:\n"
                "<code>id.atlassian.com → Security → API tokens</code>\n\n"
                "Atualize o Secret <code>JIRA_API_TOKEN</code> no GitHub."
            )
            return False
        else:
            log.warning("Verificação do token Jira retornou %s", resp.status_code)
            return True
    except Exception as e:
        log.warning("Erro ao verificar token Jira: %s", e)
        return True


def check_google_token(creds: Credentials) -> bool:
    try:
        if creds.expired or not creds.token:
            creds.refresh(Request())
        log.info("✅ Token Google Calendar válido")
        return True
    except Exception as e:
        error_msg = str(e).lower()
        if "invalid_grant" in error_msg or "token has been expired or revoked" in error_msg:
            log.error("❌ Token Google revogado: %s", e)
            send_telegram_alert(
                "🔑 <b>Clockwork Agent — Token Google Revogado</b>\n\n"
                "O token do Google Calendar foi revogado ou expirou.\n\n"
                "Para corrigir:\n"
                "1. Execute <code>python gerar_token_google.py</code> no seu PC\n"
                "2. Atualize o Secret <code>GOOGLE_CREDENTIALS_JSON</code> no GitHub\n\n"
                "O agente ficará inativo até a correção."
            )
            return False
        else:
            log.warning("Erro ao verificar token Google: %s", e)
            return True


# ── Google Chat ────────────────────────────────────────────────────────────────

def send_google_chat(text: str):
    webhook_url = os.environ.get("GOOGLE_CHAT_WEBHOOK", "")
    if not webhook_url:
        return
    try:
        clean = re.sub(r"<b>(.*?)</b>", r"*\1*", text)
        clean = re.sub(r"<code>(.*?)</code>", r"`\1`", clean)
        clean = re.sub(r"<[^>]+>", "", clean)
        resp = requests.post(webhook_url, json={"text": clean}, timeout=10)
        if not resp.ok:
            log.warning("Google Chat: falha ao enviar: %s", resp.text)
    except Exception as e:
        log.warning("Google Chat: erro de conexão: %s", e)


# ── Telegram ───────────────────────────────────────────────────────────────────

def send_telegram(text: str):
    token   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        log.warning("Telegram não configurado.")
    else:
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
                timeout=10,
            )
            if not resp.ok:
                log.warning("Telegram: falha ao enviar: %s", resp.text)
        except Exception as e:
            log.warning("Telegram: erro de conexão: %s", e)

    send_google_chat(text)


def send_telegram_alert(text: str):
    token   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if token and chat_id:
        try:
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
                timeout=10,
            )
        except Exception:
            pass

    send_google_chat(text)


def format_seconds(seconds: int) -> str:
    h = seconds // 3600
    m = (seconds % 3600) // 60
    if h and m:
        return f"{h}h{m:02d}m"
    if h:
        return f"{h}h"
    return f"{m}m"


# ── Notificações ───────────────────────────────────────────────────────────────

def notify_reminder():
    now_brt = datetime.now(BRT).strftime("%d/%m/%Y")
    send_telegram(
        f"🔔 <b>Lembrete — {now_brt}</b>\n\n"
        "Não esqueça de lançar seus eventos no Google Calendar com o código do ticket!\n\n"
        "Exemplo: <code>SCG-2098 - [JETCARD] Setup Noname</code>\n\n"
        "⏰ Os worklogs serão lançados automaticamente às 23h30."
    )


def notify_daily(launched: list[dict], skipped: list[dict], errors: list[dict], missing_seconds: int):
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
            if "não encontrado" in w["reason"]:
                lines.append("    ↳ Corrija o título do evento no Calendar e será relançado automaticamente")

    if not launched and not skipped and not errors:
        lines.append("✅ Tudo em dia! Nenhum worklog novo para lançar.")

    total_s = sum(w["duration_seconds"] for w in launched)
    if launched:
        lines.append(f"\n⏱ <b>Total lançado hoje:</b> {format_seconds(total_s)}")

    if missing_seconds > 0:
        lines.append(f"\n⚠️ <b>Horas faltantes:</b> {format_seconds(missing_seconds)} para atingir a meta de {format_seconds(DAILY_GOAL_S)}")
    else:
        lines.append(f"\n🎯 <b>Meta diária atingida!</b> {format_seconds(DAILY_GOAL_S)} lançadas.")

    send_telegram("\n".join(lines))


def notify_weekly(weekly_logs: list[dict]):
    now_brt    = datetime.now(BRT)
    start_week = (now_brt - timedelta(days=4)).strftime("%d/%m")
    end_week   = now_brt.strftime("%d/%m")

    lines = [f"📊 <b>Resumo semanal</b> — {start_week} a {end_week}\n"]

    if not weekly_logs:
        lines.append("📭 Nenhum worklog lançado esta semana.")
    else:
        by_ticket: dict[str, int] = {}
        days_with_log = set()
        for w in weekly_logs:
            by_ticket[w["issue_key"]] = by_ticket.get(w["issue_key"], 0) + w["duration_seconds"]
            days_with_log.add(w["date"])

        total_s = sum(by_ticket.values())
        for key, secs in sorted(by_ticket.items()):
            lines.append(f"✅ {key}  |  {format_seconds(secs)}")

        lines.append(f"\n⏱ <b>Total semana:</b> {format_seconds(total_s)}")
        lines.append(f"📅 <b>Dias com lançamento:</b> {len(days_with_log)} de 5")

        weekly_goal = DAILY_GOAL_S * 5
        if total_s < weekly_goal:
            lines.append(f"⚠️ <b>Faltaram:</b> {format_seconds(weekly_goal - total_s)} na semana")
        else:
            lines.append("🎯 <b>Meta semanal atingida!</b>")

    send_telegram("\n".join(lines))


# ── Feriados nacionais brasileiros ────────────────────────────────────────────

def get_national_holidays(year: int) -> set[str]:
    holidays = set()
    fixed = [
        (1, 1), (4, 21), (5, 1), (9, 7), (10, 12),
        (11, 2), (11, 15), (11, 20), (12, 25),
    ]
    for m, d in fixed:
        holidays.add(f"{year}-{m:02d}-{d:02d}")

    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m_ = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m_ + 114) // 31
    day   = ((h + l - 7 * m_ + 114) % 31) + 1
    easter = datetime(year, month, day)

    holidays.add((easter - timedelta(days=48)).strftime("%Y-%m-%d"))
    holidays.add((easter - timedelta(days=47)).strftime("%Y-%m-%d"))
    holidays.add((easter - timedelta(days=2)).strftime("%Y-%m-%d"))
    holidays.add((easter + timedelta(days=60)).strftime("%Y-%m-%d"))

    return holidays


def count_working_days(year: int, month: int) -> int:
    holidays = get_national_holidays(year)
    _, last_day = calendar.monthrange(year, month)
    count = 0
    for day in range(1, last_day + 1):
        dt = datetime(year, month, day)
        if dt.weekday() < 5 and dt.strftime("%Y-%m-%d") not in holidays:
            count += 1
    return count


def is_last_working_day_of_month(dt: datetime) -> bool:
    holidays = get_national_holidays(dt.year)
    _, last_day = calendar.monthrange(dt.year, dt.month)
    for day in range(last_day, dt.day - 1, -1):
        candidate = datetime(dt.year, dt.month, day)
        if candidate.weekday() < 5 and candidate.strftime("%Y-%m-%d") not in holidays:
            return candidate.day == dt.day
    return False


# ── Helpers Jira ───────────────────────────────────────────────────────────────

def jira_auth_header() -> str:
    email = os.environ["JIRA_EMAIL"]
    token = os.environ["JIRA_API_TOKEN"]
    return "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode()


def get_jira_worklogs_by_date(domain: str, date: str) -> int:
    jql        = f'worklogAuthor = currentUser() AND worklogDate = "{date}"'
    search_url = f"https://{domain}/rest/api/3/search/jql"
    issue_keys = []
    start_at   = 0

    while True:
        try:
            resp = requests_with_retry(
                "GET", search_url,
                headers={"Authorization": jira_auth_header(), "Accept": "application/json"},
                params={"jql": jql, "fields": "summary", "startAt": start_at, "maxResults": 50},
                timeout=15,
            )
            if resp.status_code != 200:
                log.warning("JQL horas do dia: erro %s", resp.status_code)
                break
            data   = resp.json()
            issues = data.get("issues", [])
            issue_keys.extend(i["key"] for i in issues)
            if start_at + len(issues) >= data.get("total", 0):
                break
            start_at += len(issues)
        except Exception as e:
            log.warning("JQL horas do dia: %s", e)
            break

    total_s = 0
    email   = os.environ["JIRA_EMAIL"]

    for issue_key in issue_keys:
        url = f"https://{domain}/rest/api/3/issue/{issue_key}/worklog"
        try:
            resp = requests_with_retry(
                "GET", url,
                headers={"Authorization": jira_auth_header(), "Accept": "application/json"},
                timeout=15,
            )
            if resp.status_code != 200:
                continue
            for wl in resp.json().get("worklogs", []):
                author_email = wl.get("author", {}).get("emailAddress", "")
                if (wl.get("started", "")[:10] == date
                        and author_email.lower() == email.lower()):
                    total_s += wl.get("timeSpentSeconds", 0)
        except Exception as e:
            log.warning("Erro ao buscar worklogs de %s: %s", issue_key, e)

    log.info("Total lançado no Jira em %s: %ds (%s)", date, total_s, format_seconds(total_s))
    return total_s


def get_monthly_worklogs(domain: str, year: int, month: int) -> list[dict]:
    _, last_d  = calendar.monthrange(year, month)
    date_start = f"{year}-{month:02d}-01"
    date_end   = f"{year}-{month:02d}-{last_d:02d}"

    jql        = f'worklogAuthor = currentUser() AND worklogDate >= "{date_start}" AND worklogDate <= "{date_end}"'
    search_url = f"https://{domain}/rest/api/3/search/jql"
    issue_keys = []
    start_at   = 0

    while True:
        try:
            resp = requests_with_retry(
                "GET", search_url,
                headers={"Authorization": jira_auth_header(), "Accept": "application/json"},
                params={"jql": jql, "fields": "summary", "startAt": start_at, "maxResults": 50},
                timeout=15,
            )
            if resp.status_code != 200:
                log.error("Erro na busca JQL: %s %s", resp.status_code, resp.text)
                break
            data   = resp.json()
            issues = data.get("issues", [])
            issue_keys.extend(i["key"] for i in issues)
            if start_at + len(issues) >= data.get("total", 0):
                break
            start_at += len(issues)
        except Exception as e:
            log.warning("Erro na busca JQL: %s", e)
            break

    log.info("JQL retornou %d ticket(s) com worklogs em %s/%s", len(issue_keys), month, year)

    month_str = f"{year}-{month:02d}"
    result    = []
    email     = os.environ["JIRA_EMAIL"]

    for issue_key in issue_keys:
        url = f"https://{domain}/rest/api/3/issue/{issue_key}/worklog"
        try:
            resp = requests_with_retry(
                "GET", url,
                headers={"Authorization": jira_auth_header(), "Accept": "application/json"},
                timeout=15,
            )
            if resp.status_code != 200:
                continue
            for wl in resp.json().get("worklogs", []):
                author_email = wl.get("author", {}).get("emailAddress", "")
                if (wl.get("started", "").startswith(month_str)
                        and author_email.lower() == email.lower()):
                    result.append({
                        "issue_key":        issue_key,
                        "duration_seconds": wl.get("timeSpentSeconds", 0),
                        "date":             wl.get("started", "")[:10],
                    })
        except Exception as e:
            log.warning("Erro ao buscar worklogs de %s: %s", issue_key, e)

    return result


def notify_monthly(domain: str, year: int, month: int):
    month_name = [
        "Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho",
        "Julho", "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro"
    ][month - 1]

    working_days = count_working_days(year, month)
    goal_s       = working_days * DAILY_GOAL_S
    worklogs     = get_monthly_worklogs(domain, year, month)

    by_ticket: dict[str, int] = {}
    for wl in worklogs:
        by_ticket[wl["issue_key"]] = by_ticket.get(wl["issue_key"], 0) + wl["duration_seconds"]

    total_s   = sum(by_ticket.values())
    diff_s    = total_s - goal_s
    goal_h    = goal_s  / 3600
    total_h   = total_s / 3600
    diff_h    = diff_s  / 3600
    diff_sign = "+" if diff_h >= 0 else ""

    lines = [f"📅 <b>Relatório Mensal — {month_name}/{year}</b>\n"]
    lines.append(
        f"Meta: <b>{goal_h:.2f}h</b>  |  "
        f"Reg: <b>{total_h:.2f}h</b>  |  "
        f"Dif: <b>{diff_sign}{diff_h:.2f}h</b> {'🎯' if diff_h >= 0 else '⚠️'}"
    )
    lines.append(f"📆 Dias úteis: {working_days}")

    if by_ticket:
        lines.append("\n📋 <b>Por ticket:</b>")
        for key, secs in sorted(by_ticket.items(), key=lambda x: -x[1]):
            lines.append(f"  • {key}  |  {format_seconds(secs)}")

    send_telegram("\n".join(lines))


# ── Helpers Google Calendar ────────────────────────────────────────────────────

def build_calendar_service():
    creds_json = os.environ["GOOGLE_CREDENTIALS_JSON"]
    token_data = json.loads(creds_json)

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

    return build("calendar", "v3", credentials=creds, cache_discovery=False), creds


def get_recent_events(service) -> list[dict]:
    now_brt        = datetime.now(BRT)
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
    now_brt = datetime.now(BRT)
    monday  = (now_brt - timedelta(days=now_brt.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    result = service.events().list(
        calendarId="primary",
        timeMin=monday.isoformat(),
        timeMax=now_brt.isoformat(),
        singleEvents=True,
        orderBy="startTime",
    ).execute()

    return result.get("items", [])


def parse_event(event: dict) -> dict | None:
    title = event.get("summary", "")
    match = TICKET_PATTERN.search(title)
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

    comment = re.sub(r"^[A-Z]+-\d+\s*[-–]\s*", "", title).strip() or title

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


def already_logged_in_jira(domain: str, issue_key: str, date: str, duration_seconds: int) -> bool:
    url = f"https://{domain}/rest/api/3/issue/{issue_key}/worklog"
    try:
        resp = requests_with_retry(
            "GET", url,
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
        log.warning("Erro ao verificar worklogs em %s: %s", issue_key, e)
    return False


def get_jira_worklogs_today(domain: str, issue_key: str, today: str) -> list[dict]:
    url = f"https://{domain}/rest/api/3/issue/{issue_key}/worklog"
    try:
        resp = requests_with_retry(
            "GET", url,
            headers={"Authorization": jira_auth_header(), "Accept": "application/json"},
            timeout=15,
        )
        if resp.status_code != 200:
            return []
        return [wl for wl in resp.json().get("worklogs", [])
                if wl.get("started", "")[:10] == today]
    except Exception:
        return []


def delete_jira_worklog(domain: str, issue_key: str, worklog_id: str) -> bool:
    url = f"https://{domain}/rest/api/3/issue/{issue_key}/worklog/{worklog_id}"
    try:
        resp = requests_with_retry(
            "DELETE", url,
            headers={"Authorization": jira_auth_header()},
            timeout=15,
        )
        if resp.status_code == 204:
            log.info("🗑️  Worklog removido: %s | id=%s", issue_key, worklog_id)
            return True
        else:
            log.error("❌ Falha ao remover worklog %s: %s", worklog_id, resp.status_code)
            return False
    except Exception as e:
        log.error("Erro ao remover worklog %s: %s", worklog_id, e)
        return False


def log_worklog(domain: str, parsed: dict):
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

    resp = requests_with_retry(
        "POST", url,
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
        return True, None
    elif resp.status_code == 404:
        log.error("❌ Ticket não encontrado no Jira: %s", parsed["issue_key"])
        return False, "ticket_not_found"
    elif resp.status_code == 401:
        log.error("❌ Não autorizado — verifique JIRA_EMAIL e JIRA_API_TOKEN")
        return False, "unauthorized"
    elif resp.status_code == 403:
        log.error("❌ Sem permissão para lançar worklog em %s", parsed["issue_key"])
        return False, "forbidden"
    else:
        log.error("❌ Falha ao lançar worklog em %s: %s %s",
                  parsed["issue_key"], resp.status_code, resp.text)
        return False, "api_error"


# ── Controle de duplicatas ─────────────────────────────────────────────────────

def load_logged() -> dict:
    if LOGGED_FILE.exists():
        data = json.loads(LOGGED_FILE.read_text())
        if isinstance(data.get("logged_event_ids"), list):
            return {eid: None for eid in data["logged_event_ids"]}
        return data.get("logged_worklogs", {})
    return {}


def save_logged(logged: dict):
    LOGGED_FILE.write_text(json.dumps({"logged_worklogs": logged}, indent=2))


# ── Cancelamento de worklogs ───────────────────────────────────────────────────

def process_cancellations(domain: str, logged: dict, current_event_ids: set[str]) -> tuple[dict, list[str]]:
    cancelled = []

    for event_id, worklog_id in list(logged.items()):
        if event_id in current_event_ids:
            continue

        if worklog_id is None:
            del logged[event_id]
            continue

        if ":" not in str(worklog_id):
            del logged[event_id]
            continue

        issue_key, wl_id = worklog_id.split(":", 1)
        log.info("🗑️  Evento deletado do Calendar detectado: %s | worklog=%s", issue_key, wl_id)

        if delete_jira_worklog(domain, issue_key, wl_id):
            cancelled.append(issue_key)

        del logged[event_id]

    return logged, cancelled


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    domain  = os.environ["JIRA_DOMAIN"]
    now_brt = datetime.now(BRT)

    log.info("=== Jira Clockwork Agent iniciado === (%s BRT)", now_brt.strftime("%H:%M"))

    # ── Determina modo de execução ────────────────────────────────────────────
    force_mode = os.environ.get("FORCE_MODE", "").strip().lower()
    mode       = get_execution_mode(now_brt, force_mode)
    log.info("Modo: %s", mode)

    if mode == "skip":
        log.info("Fora da janela de execução — encerrando.")
        return

    # ── Constrói serviço Google Calendar ─────────────────────────────────────
    try:
        service, creds = build_calendar_service()
    except Exception as e:
        msg = (f"🚨 <b>Clockwork Agent — ERRO CRÍTICO</b>\n\n"
               f"Falha ao conectar com Google Calendar:\n<code>{e}</code>")
        send_telegram_alert(msg)
        save_health("error", error=str(e))
        raise

    # ── Verificação preventiva de tokens ─────────────────────────────────────
    log.info("Verificando tokens...")
    google_ok = check_google_token(creds)
    jira_ok   = check_jira_token(domain)

    if not google_ok or not jira_ok:
        save_health("error", error="Token inválido detectado na verificação preventiva")
        return

    # ── Modo lembrete (18h) ───────────────────────────────────────────────────
    if mode == "reminder":
        log.info("Enviando lembrete das 18h...")
        notify_reminder()
        save_health("ok")
        return

    # ── Modo lançamento (23h30) ───────────────────────────────────────────────
    try:
        # Se após meia-noite, usa a data de ontem para os relatórios
        launch_date = now_brt
        if now_brt.hour == 0:
            launch_date = now_brt - timedelta(days=1)
            log.info("Após meia-noite — usando data de ontem: %s", launch_date.date())

        events            = get_recent_events(service)
        log.info("%d evento(s) encontrado(s) no período.", len(events))

        parsed_events     = [p for e in events if (p := parse_event(e)) is not None]
        current_event_ids = {e["id"] for e in events}
        log.info("%d evento(s) com ticket no título.", len(parsed_events))

        logged         = load_logged()
        already_logged = set(logged.keys())
        new_logged     = dict(logged)

        new_logged, cancelled = process_cancellations(domain, new_logged, current_event_ids)
        if cancelled:
            log.info("🗑️  %d worklog(s) cancelado(s): %s", len(cancelled), cancelled)

        launched = []
        skipped  = []
        errors   = []

        for parsed in parsed_events:
            eid = parsed["event_id"]

            if eid in already_logged:
                log.info("⏭️  Já lançado pelo agente: %s (%s)", parsed["issue_key"], eid[:8])
                continue

            if already_logged_in_jira(domain, parsed["issue_key"], parsed["date"], parsed["duration_seconds"]):
                log.info("⏭️  Worklog manual detectado: %s | %s", parsed["issue_key"], parsed["date"])
                skipped.append(parsed)
                new_logged[eid] = None
                continue

            success, error_type = log_worklog(domain, parsed)
            if success:
                wl_id = None
                try:
                    jira_wls = get_jira_worklogs_today(domain, parsed["issue_key"], parsed["date"])
                    for wl in reversed(jira_wls):
                        if abs(wl.get("timeSpentSeconds", 0) - parsed["duration_seconds"]) <= TOLERANCE_S:
                            wl_id = f"{parsed['issue_key']}:{wl['id']}"
                            break
                except Exception:
                    pass

                new_logged[eid] = wl_id
                launched.append(parsed)
            else:
                error_messages = {
                    "ticket_not_found": f"Ticket {parsed['issue_key']} não encontrado no Jira — verifique o título do evento",
                    "unauthorized":     "Credenciais inválidas — verifique JIRA_EMAIL e JIRA_API_TOKEN",
                    "forbidden":        f"Sem permissão para lançar worklog em {parsed['issue_key']}",
                    "api_error":        "Falha na API do Jira",
                }
                reason = error_messages.get(error_type, "Erro desconhecido")

                if error_type != "ticket_not_found":
                    new_logged[eid] = None

                errors.append({**parsed, "reason": reason})

        save_logged(new_logged)

        today_str     = launch_date.date().isoformat()
        total_today_s = get_jira_worklogs_by_date(domain, today_str)
        missing_s     = max(0, DAILY_GOAL_S - total_today_s)

        notify_daily(launched, skipped, errors, missing_s)

        if cancelled:
            send_telegram(
                "🗑️ <b>Worklogs cancelados</b>\n\n"
                + "\n".join(f"  • {k}" for k in cancelled)
                + "\n\nOs eventos foram removidos do Calendar e os worklogs foram deletados no Jira."
            )

        # Relatório semanal — toda sexta-feira
        if launch_date.weekday() == 4:
            log.info("Sexta-feira — gerando relatório semanal...")
            week_events = get_week_events(service)
            weekly_logs = [p for e in week_events if (p := parse_event(e)) is not None
                           and e["id"] in new_logged]
            notify_weekly(weekly_logs)

        # Relatório mensal — último dia útil do mês
        force_monthly = os.environ.get("FORCE_MONTHLY", "").strip().lower() == "true"
        if is_last_working_day_of_month(launch_date) or force_monthly:
            log.info("Gerando relatório mensal...")
            notify_monthly(domain, launch_date.year, launch_date.month)

        save_health("ok", launched=len(launched))

        log.info("=== Concluído: %d worklog(s) lançado(s) ===", len(launched))

    except Exception as e:
        log.error("Erro inesperado: %s", e)
        send_telegram_alert(
            f"🚨 <b>Clockwork Agent — ERRO</b>\n\n"
            f"Ocorreu um erro inesperado durante a execução:\n<code>{e}</code>\n\n"
            f"Verifique o log no GitHub Actions."
        )
        save_health("error", error=str(e))
        raise


if __name__ == "__main__":
    main()