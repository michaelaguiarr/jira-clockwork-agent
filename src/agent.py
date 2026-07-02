"""
Jira Clockwork Agent
Lê eventos do Google Calendar e lança worklogs no Jira (sincronizado com Clockwork Pro).
Roda via GitHub Actions:
  - 18h BRT: lembrete para lançar eventos no Calendar
  - 20h BRT: lança worklogs + verifica horas faltantes + relatório semanal (sextas)
"""

import os
import re
import json
import base64
import logging
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
# Qualquer padrão PROJ-XXXX (ex: SCG-1234, CARDS-567, HPAY-890)
TICKET_PATTERN = re.compile(r"\b([A-Z]+-\d+)\b")
LOGGED_FILE    = Path("logged_worklogs.json")
BRT            = timezone(timedelta(hours=-3))
TOLERANCE_S    = 300    # 5 min de tolerância ao comparar duração
DAILY_GOAL_S   = int(os.environ.get("DAILY_HOURS_GOAL", "8")) * 3600


# ── Telegram ───────────────────────────────────────────────────────────────────

def send_telegram(text: str):
    """Envia mensagem via Telegram. Falha silenciosa para não quebrar o agente."""
    token   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        log.warning("Telegram não configurado.")
        return
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


def send_telegram_alert(text: str):
    """Envia alerta de falha via Telegram. Tenta mesmo em caso de erro crítico."""
    token   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception:
        pass


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
    """Lembrete das 18h para lançar eventos no Calendar."""
    now_brt = datetime.now(BRT).strftime("%d/%m/%Y")
    send_telegram(
        f"🔔 <b>Lembrete — {now_brt}</b>\n\n"
        "Não esqueça de lançar seus eventos no Google Calendar com o código do ticket!\n\n"
        "Exemplo: <code>SCG-2098 - [JETCARD] Setup Noname</code>\n\n"
        "⏰ Os worklogs serão lançados automaticamente às 20h."
    )


def notify_daily(launched: list[dict], skipped: list[dict], errors: list[dict], missing_seconds: int):
    """Notificação das 20h com resumo do lançamento e horas faltantes."""
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
                lines.append(f"    ↳ Corrija o título do evento no Calendar e será relançado automaticamente")

    if not launched and not skipped and not errors:
        lines.append("✅ Tudo em dia! Nenhum worklog novo para lançar.")

    total_s = sum(w["duration_seconds"] for w in launched)
    if launched:
        lines.append(f"\n⏱ <b>Total lançado hoje:</b> {format_seconds(total_s)}")

    # Aviso de horas faltantes
    if missing_seconds > 0:
        lines.append(f"\n⚠️ <b>Horas faltantes:</b> {format_seconds(missing_seconds)} para atingir a meta de {format_seconds(DAILY_GOAL_S)}")
    else:
        lines.append(f"\n🎯 <b>Meta diária atingida!</b> {format_seconds(DAILY_GOAL_S)} lançadas.")

    send_telegram("\n".join(lines))


def notify_weekly(weekly_logs: list[dict]):
    """Relatório semanal toda sexta-feira às 20h."""
    now_brt    = datetime.now(BRT)
    start_week = (now_brt - timedelta(days=4)).strftime("%d/%m")
    end_week   = now_brt.strftime("%d/%m")

    lines = [f"📊 <b>Resumo semanal</b> — {start_week} a {end_week}\n"]

    if not weekly_logs:
        lines.append("📭 Nenhum worklog lançado esta semana.")
    else:
        # Agrupar por ticket
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

        # Meta semanal (8h × 5 dias)
        weekly_goal = DAILY_GOAL_S * 5
        if total_s < weekly_goal:
            lines.append(f"⚠️ <b>Faltaram:</b> {format_seconds(weekly_goal - total_s)} na semana")
        else:
            lines.append(f"🎯 <b>Meta semanal atingida!</b>")

    send_telegram("\n".join(lines))


# ── Feriados nacionais brasileiros ────────────────────────────────────────────

def get_national_holidays(year: int) -> set[str]:
    """
    Retorna feriados nacionais fixos + Carnaval, Sexta-feira Santa e Corpus Christi.
    Formato: YYYY-MM-DD
    """
    holidays = set()

    # Feriados fixos
    fixed = [
        (1, 1),   # Ano Novo
        (4, 21),  # Tiradentes
        (5, 1),   # Dia do Trabalho
        (9, 7),   # Independência
        (10, 12), # Nossa Senhora Aparecida
        (11, 2),  # Finados
        (11, 15), # Proclamação da República
        (11, 20), # Consciência Negra (Lei 14.759/2023)
        (12, 25), # Natal
    ]
    for m, d in fixed:
        holidays.add(f"{year}-{m:02d}-{d:02d}")

    # Páscoa (algoritmo de Butcher)
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

    # Carnaval (segunda e terça, 48 e 47 dias antes da Páscoa)
    holidays.add((easter - timedelta(days=48)).strftime("%Y-%m-%d"))
    holidays.add((easter - timedelta(days=47)).strftime("%Y-%m-%d"))
    # Sexta-feira Santa (2 dias antes da Páscoa)
    holidays.add((easter - timedelta(days=2)).strftime("%Y-%m-%d"))
    # Corpus Christi (60 dias após a Páscoa)
    holidays.add((easter + timedelta(days=60)).strftime("%Y-%m-%d"))

    return holidays


def count_working_days(year: int, month: int) -> int:
    """Conta dias úteis do mês excluindo fins de semana e feriados nacionais."""
    holidays = get_national_holidays(year)
    _, last_day = calendar.monthrange(year, month)
    count = 0
    for day in range(1, last_day + 1):
        dt = datetime(year, month, day)
        date_str = dt.strftime("%Y-%m-%d")
        if dt.weekday() < 5 and date_str not in holidays:  # seg-sex e não feriado
            count += 1
    return count


def is_last_working_day_of_month(dt: datetime) -> bool:
    """Verifica se hoje é o último dia útil do mês."""
    holidays = get_national_holidays(dt.year)
    _, last_day = calendar.monthrange(dt.year, dt.month)

    for day in range(last_day, dt.day - 1, -1):
        candidate = datetime(dt.year, dt.month, day)
        date_str  = candidate.strftime("%Y-%m-%d")
        if candidate.weekday() < 5 and date_str not in holidays:
            return candidate.day == dt.day

    return False


# ── Relatório mensal ───────────────────────────────────────────────────────────

def get_monthly_worklogs(domain: str, year: int, month: int) -> list[dict]:
    """
    Busca TODOS os worklogs lançados pelo usuário no mês via JQL.
    Inclui lançamentos manuais, via agente e sem evento no Calendar.
    """
    _, last_d  = calendar.monthrange(year, month)
    date_start = f"{year}-{month:02d}-01"
    date_end   = f"{year}-{month:02d}-{last_d:02d}"

    # JQL: todos os issues onde o usuário lançou horas no mês
    jql = f'worklogAuthor = currentUser() AND worklogDate >= "{date_start}" AND worklogDate <= "{date_end}"'

    # Busca os issues via JQL
    search_url = f"https://{domain}/rest/api/3/search"
    issue_keys = []
    start_at   = 0

    while True:
        try:
            resp = requests.get(
                search_url,
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

    # Busca os worklogs de cada issue no mês
    month_str = f"{year}-{month:02d}"
    result    = []

    for issue_key in issue_keys:
        url = f"https://{domain}/rest/api/3/issue/{issue_key}/worklog"
        try:
            resp = requests.get(
                url,
                headers={"Authorization": jira_auth_header(), "Accept": "application/json"},
                timeout=15,
            )
            if resp.status_code != 200:
                continue

            # Filtra só worklogs do usuário atual no mês
            email = os.environ["JIRA_EMAIL"]
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
    """Envia relatório mensal no Telegram."""
    month_name = [
        "Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho",
        "Julho", "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro"
    ][month - 1]

    working_days = count_working_days(year, month)
    goal_s       = working_days * DAILY_GOAL_S
    worklogs     = get_monthly_worklogs(domain, year, month)

    # Agrupa por ticket
    by_ticket: dict[str, int] = {}
    for wl in worklogs:
        by_ticket[wl["issue_key"]] = by_ticket.get(wl["issue_key"], 0) + wl["duration_seconds"]

    total_s = sum(by_ticket.values())
    diff_s  = total_s - goal_s

    lines = [f"📅 <b>Relatório Mensal — {month_name}/{year}</b>\n"]

    # Linha resumo no formato solicitado
    goal_h  = goal_s  / 3600
    total_h = total_s / 3600
    diff_h  = diff_s  / 3600
    diff_sign = "+" if diff_h >= 0 else ""
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

    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def get_recent_events(service) -> list[dict]:
    """Retorna eventos entre START_DATE (ou LOOKBACK_DAYS atrás) e agora."""
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


def get_today_events(service) -> list[dict]:
    """Retorna todos os eventos de hoje (para calcular total de horas)."""
    now_brt   = datetime.now(BRT)
    start_brt = now_brt.replace(hour=0, minute=0, second=0, microsecond=0)

    result = service.events().list(
        calendarId="primary",
        timeMin=start_brt.isoformat(),
        timeMax=now_brt.isoformat(),
        singleEvents=True,
        orderBy="startTime",
    ).execute()

    return result.get("items", [])


def get_week_events(service) -> list[dict]:
    """Retorna eventos da semana atual (seg a hoje) para o relatório semanal."""
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
        log.warning("Erro ao verificar worklogs em %s: %s", issue_key, e)
    return False


def get_jira_worklogs_today(domain: str, issue_key: str, today: str) -> list[dict]:
    """Retorna worklogs do ticket para hoje (para verificar cancelamentos)."""
    url = f"https://{domain}/rest/api/3/issue/{issue_key}/worklog"
    try:
        resp = requests.get(
            url,
            headers={"Authorization": jira_auth_header(), "Accept": "application/json"},
            timeout=15,
        )
        if resp.status_code != 200:
            return []
        return [
            wl for wl in resp.json().get("worklogs", [])
            if wl.get("started", "")[:10] == today
        ]
    except Exception:
        return []


def delete_jira_worklog(domain: str, issue_key: str, worklog_id: str) -> bool:
    """Remove um worklog do Jira."""
    url = f"https://{domain}/rest/api/3/issue/{issue_key}/worklog/{worklog_id}"
    try:
        resp = requests.delete(
            url,
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
    """Carrega o controle de worklogs lançados. Formato: {event_id: worklog_id}"""
    if LOGGED_FILE.exists():
        data = json.loads(LOGGED_FILE.read_text())
        # Suporte ao formato antigo (lista de IDs)
        if isinstance(data.get("logged_event_ids"), list):
            return {eid: None for eid in data["logged_event_ids"]}
        return data.get("logged_worklogs", {})
    return {}


def save_logged(logged: dict):
    LOGGED_FILE.write_text(json.dumps({"logged_worklogs": logged}, indent=2))


# ── Cancelamento de worklogs ───────────────────────────────────────────────────

def process_cancellations(domain: str, logged: dict, current_event_ids: set[str]) -> tuple[dict, list[str]]:
    """
    Detecta eventos que foram deletados do Calendar mas têm worklog lançado no Jira.
    Remove o worklog e atualiza o controle local.
    Retorna (logged atualizado, lista de tickets cancelados para notificação).
    """
    cancelled = []
    today     = datetime.now(BRT).date().isoformat()

    for event_id, worklog_id in list(logged.items()):
        if event_id in current_event_ids:
            continue  # evento ainda existe no Calendar

        if worklog_id is None:
            # Lançamento antigo sem worklog_id salvo — não consegue cancelar
            del logged[event_id]
            continue

        # Tenta remover o worklog do Jira
        # Precisamos saber o issue_key — está embutido no worklog_id como "KEY:ID"
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
    hour    = now_brt.hour

    log.info("=== Jira Clockwork Agent iniciado === (hora BRT: %d)", hour)

    try:
        service = build_calendar_service()
    except Exception as e:
        msg = f"🚨 <b>Clockwork Agent — ERRO CRÍTICO</b>\n\nFalha ao conectar com Google Calendar:\n<code>{e}</code>"
        send_telegram_alert(msg)
        raise

    # ── Execução das 18h: só lembrete ────────────────────────────────────────
    force_mode = os.environ.get("FORCE_MODE", "").strip().lower()
    is_reminder = hour < 19 and force_mode != "launch"
    is_launch   = hour >= 19 or force_mode == "launch"

    if is_reminder and not is_launch:
        log.info("Execução das 18h — enviando lembrete.")
        notify_reminder()
        return

    # ── Execução das 20h: lançamento + verificações ───────────────────────────
    try:
        events = get_recent_events(service)
        log.info("%d evento(s) encontrado(s) no período.", len(events))

        parsed_events    = [p for e in events if (p := parse_event(e)) is not None]
        current_event_ids = {e["id"] for e in events}
        log.info("%d evento(s) com ticket no título.", len(parsed_events))

        logged         = load_logged()
        already_logged = set(logged.keys())
        new_logged     = dict(logged)

        # Cancelamentos — eventos deletados do Calendar
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
                # Busca o worklog_id recém criado para poder cancelar depois se necessário
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
                # Mapeia o tipo de erro para mensagem amigável
                error_messages = {
                    "ticket_not_found": f"Ticket {parsed['issue_key']} não encontrado no Jira — verifique o título do evento",
                    "unauthorized":     "Credenciais inválidas — verifique JIRA_EMAIL e JIRA_API_TOKEN",
                    "forbidden":        f"Sem permissão para lançar worklog em {parsed['issue_key']}",
                    "api_error":        "Falha na API do Jira",
                }
                reason = error_messages.get(error_type, "Erro desconhecido")

                # Ticket não encontrado: NÃO marca como processado para tentar de novo
                if error_type != "ticket_not_found":
                    new_logged[eid] = None

                errors.append({**parsed, "reason": reason})

        save_logged(new_logged)

        # Calcular horas lançadas hoje (lançadas agora + já existentes)
        today_events   = get_today_events(service)
        today_parsed   = [p for e in today_events if (p := parse_event(e)) is not None]
        total_today_s  = sum(p["duration_seconds"] for p in today_parsed
                             if p["event_id"] in new_logged)
        missing_s      = max(0, DAILY_GOAL_S - total_today_s)

        # Notificação diária — sempre envia, mesmo sem novidades
        notify_daily(launched, skipped, errors, missing_s)

        # Notificação de cancelamentos
        if cancelled:
            send_telegram(
                f"🗑️ <b>Worklogs cancelados</b>\n\n"
                + "\n".join(f"  • {k}" for k in cancelled)
                + "\n\nOs eventos foram removidos do Calendar e os worklogs foram deletados no Jira."
            )

        # Relatório semanal — toda sexta-feira às 20h
        if now_brt.weekday() == 4:
            log.info("Sexta-feira — gerando relatório semanal...")
            week_events = get_week_events(service)
            weekly_logs = [p for e in week_events if (p := parse_event(e)) is not None
                           and e["id"] in new_logged]
            notify_weekly(weekly_logs)

        # Relatório mensal — último dia útil do mês às 20h (ou FORCE_MONTHLY=true)
        force_monthly = os.environ.get("FORCE_MONTHLY", "").strip().lower() == "true"
        if is_last_working_day_of_month(now_brt) or force_monthly:
            log.info("Gerando relatório mensal...")
            notify_monthly(domain, now_brt.year, now_brt.month)

        log.info("=== Concluído: %d worklog(s) lançado(s) ===", len(launched))

    except Exception as e:
        log.error("Erro inesperado: %s", e)
        send_telegram_alert(
            f"🚨 <b>Clockwork Agent — ERRO</b>\n\n"
            f"Ocorreu um erro inesperado durante a execução:\n<code>{e}</code>\n\n"
            f"Verifique o log no GitHub Actions."
        )
        raise


if __name__ == "__main__":
    main()