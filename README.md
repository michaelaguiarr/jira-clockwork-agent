# ⏱️ Jira Clockwork Agent

Agente que lê automaticamente seus eventos do **Google Calendar** e lança os worklogs no **Jira** (sincronizado com o **Clockwork Pro**), sem você precisar fazer nada.

Ao final de cada execução, você recebe notificações no **Telegram** e no **Google Chat** com o resumo do que foi lançado. Toda sexta-feira às 20h recebe o **relatório semanal**, e no último dia útil do mês o **relatório mensal** com meta x realizado.

> Criado para o time de Sustentação da RPE Processadora — mas funciona para qualquer equipe que use Jira + Clockwork Pro + Google Calendar.

---

## Como funciona

```
GitHub Actions (18h e 20h, seg–sex)
  ↓
18h → Lembrete no Telegram/Google Chat para lançar eventos no Calendar
  ↓
20h → Google Calendar API
        → busca eventos do período configurado
        → filtra os que têm PROJ-XXXX no título (SCG-1234, CARDS-567, etc.)
      Jira API
        → verifica se já existe worklog no ticket (evita duplicar lançamentos manuais)
        → lança worklog com a duração exata do evento
        → retry automático (até 3x) em caso de falha transitória
      Telegram + Google Chat
        → notificação diária com worklogs lançados e horas faltantes
        → relatório semanal toda sexta às 20h
        → relatório mensal no último dia útil do mês
      logged_worklogs.json + health.json
        → controle de eventos processados e status da última execução
```

---

## Convenção dos eventos no Calendar

O título do evento deve conter o código do ticket em qualquer posição. Qualquer padrão `PROJ-XXXX` é reconhecido automaticamente:

```
✅ SCG-2098 - [JETCARD] - Setup Noname
✅ CARDS-567 - Investigação bug pagamento
✅ [REUNIÃO] SCG-2050 - Alinhamento time
✅ HPAY-890 - Correção HP Vencimento
❌ Daily Sustentação          ← sem ticket, será ignorado
❌ Almoço                     ← sem ticket, será ignorado
```

O comentário do worklog no Jira será o título do evento **sem o prefixo `PROJ-XXXX -`**.

> ⚠️ Eventos de **dia inteiro** são ignorados. Apenas eventos com horário definido (início e fim) são processados.

---

## Notificações

**Lembrete diário (18h):**

```
🔔 Lembrete — 02/07/2026

Não esqueça de lançar seus eventos no Google Calendar com o código do ticket!
Exemplo: SCG-2098 - [JETCARD] Setup Noname
⏰ Os worklogs serão lançados automaticamente às 20h.
```

**Execução diária (20h):**

```
⏱ Clockwork Agent — 02/07/2026 20:00

✅ Lançados:
  • SCG-2098  |  1h00  |  [JETCARD] Setup Noname

⏭ Já existiam no Jira:
  • SCG-1349  |  2026-07-02

⏱ Total lançado hoje: 1h00
⚠️ Horas faltantes: 2h00 para atingir a meta de 8h
```

**Relatório semanal (toda sexta às 20h):**

```
📊 Resumo semanal — 30/06 a 04/07

✅ SCG-2098  |  16h00
✅ SCG-1957  |  8h00
✅ CARDS-567  |  16h00

⏱ Total semana: 40h00
📅 Dias com lançamento: 5 de 5
🎯 Meta semanal atingida!
```

**Relatório mensal (último dia útil do mês às 20h):**

```
📅 Relatório Mensal — Julho/2026

Meta: 184.00h  |  Reg: 180.50h  |  Dif: -3.50h ⚠️
📆 Dias úteis: 23

📋 Por ticket:
  • SCG-2098  |  80h
  • CARDS-567  |  60h
  • SCG-1957  |  40h30m
```

---

## Setup

### 1. Fork / criar o repositório

Crie um repositório **privado** no GitHub e suba este projeto.

> ⚠️ Privado é importante — o repositório armazena credenciais e o controle de worklogs lançados.

---

### 2. Criar o bot no Telegram

1. Abra o Telegram e acesse [@BotFather](https://t.me/BotFather)
2. Digite `/newbot`
3. Dê um nome para o bot (ex: `Clockwork Agent`)
4. Dê um username (ex: `clockwork_rpe_bot`) — precisa terminar em `bot`
5. O BotFather vai enviar o **token** do bot — guarde-o

**Pegar o Chat ID:**

1. Mande qualquer mensagem para o bot recém-criado no Telegram
2. Acesse no browser (substituindo `SEU_TOKEN` pelo token recebido):

```
https://api.telegram.org/botSEU_TOKEN/getUpdates
```

3. Copie o número do campo `"id"` dentro de `"chat"`:

```json
"chat": { "id": 123456789, ... }
```

---

### 3. Configurar o Google Chat (opcional)

Todas as notificações são espelhadas automaticamente no Google Chat via Webhook.

1. Abra o espaço (room) no Google Chat onde quer receber as notificações
2. Clique no nome do espaço → **Apps & integrations → Add webhooks**
3. Dê um nome (ex: `Clockwork Agent`) → **Save**
4. Copie a URL gerada e adicione como Secret `GOOGLE_CHAT_WEBHOOK`

---

### 4. Ativar a Google Calendar API

1. Acesse [console.cloud.google.com](https://console.cloud.google.com)
2. Crie um projeto (ex: `clockwork-agent`)
3. Vá em **APIs & Services → Library** → busque **"Google Calendar API"** → **Enable**
4. Vá em **APIs & Services → Credentials → OAuth consent screen**
   - User Type: **External** → Create
   - Preencha nome do app e e-mail de suporte → salve e avance até o fim
5. Volte em **Credentials → Create Credentials → OAuth client ID**
   - Application type: **Desktop app**
   - Baixe o JSON e renomeie para `credentials.json`

---

### 5. Gerar o token OAuth do Google (uma vez no seu PC)

Com o `credentials.json` na raiz do projeto:

```bash
# Crie o ambiente virtual
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

# Instale a dependência
pip install google-auth-oauthlib

# Gere o token
python gerar_token_google.py
```

O browser vai abrir pedindo autorização. Após autorizar, o script salva o `google_token.json`.

> Se aparecer aviso **"Google não verificou este app"** → clique em **Avançado → Acessar (não seguro)**. É normal para apps em desenvolvimento.

Para copiar o conteúdo do token direto pro clipboard (macOS):

```bash
cat google_token.json | pbcopy
```

---

### 6. Criar token de API do Jira

1. Acesse [id.atlassian.com/manage-profile/security/api-tokens](https://id.atlassian.com/manage-profile/security/api-tokens)
2. Clique em **Create API token**
3. Dê um nome (ex: `clockwork-agent`) e copie o token gerado

---

### 7. Configurar Secrets no GitHub

No repositório: **Settings → Secrets and variables → Actions → New repository secret**

| Secret                    | Valor                                               | Exemplo                           |
| ------------------------- | --------------------------------------------------- | --------------------------------- |
| `JIRA_DOMAIN`             | Domínio do seu Jira (sem https://)                  | `suaempresa.atlassian.net`        |
| `JIRA_EMAIL`              | E-mail da sua conta Atlassian                       | `voce@empresa.com`                |
| `JIRA_API_TOKEN`          | Token gerado no passo 6                             | `ATATxxxxxxxx`                    |
| `GOOGLE_CREDENTIALS_JSON` | Conteúdo do `google_token.json`                     | `{"token": "...", ...}`           |
| `TELEGRAM_BOT_TOKEN`      | Token do bot criado no passo 2                      | `123456:ABCdef...`                |
| `TELEGRAM_CHAT_ID`        | ID do seu chat com o bot                            | `388676023`                       |
| `GOOGLE_CHAT_WEBHOOK`     | URL do webhook do Google Chat (opcional)            | `https://chat.googleapis.com/...` |
| `START_DATE`              | Data de corte — eventos anteriores são ignorados    | `2026-07-01`                      |
| `LOOKBACK_DAYS`           | Dias para trás na busca (usado se START_DATE vazio) | `7`                               |
| `DAILY_HOURS_GOAL`        | Meta diária de horas                                | `8`                               |
| `FORCE_MODE`              | `launch` para forçar lançamento fora do horário     | _(vazio)_                         |
| `FORCE_MONTHLY`           | `true` para forçar relatório mensal                 | _(vazio)_                         |

> **Dica:** `START_DATE` deve ser o dia em que você começou a usar o agente, para evitar duplicar lançamentos manuais anteriores.

---

### 8. Testar manualmente

No GitHub: **Actions → Clockwork Agent → Run workflow**

Para testar o fluxo completo de lançamento fora do horário, defina `FORCE_MODE=launch` temporariamente e volte para vazio após o teste.

Exemplo de log esperado:

```
=== Jira Clockwork Agent iniciado === (hora BRT: 20)
Data de corte (START_DATE): 2026-07-01
Buscando eventos de 2026-07-01T00:00:00-03:00 até 2026-07-02T20:00:00-03:00
7 evento(s) encontrado(s) no período.
3 evento(s) com ticket no título.
⏭️  Já lançado pelo agente: SCG-2098 (abc12345)
⚠️  Worklog já existe no Jira: SCG-1957 | 2026-07-01 | 5400s
✅ Worklog lançado: SCG-2050 | 3600s | 'Alinhamento time'
Total lançado no Jira em 2026-07-02: 21600s (6h)
Health check salvo: status=ok worklogs=1
=== Concluído: 1 worklog(s) lançado(s) ===
```

---

## Proteções contra duplicata

O agente tem **duas camadas** de proteção:

| Camada                             | Como funciona                                                                                                                                                                |
| ---------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Local** (`logged_worklogs.json`) | Registra o ID de cada evento do Calendar já processado. Se o agente já lançou, não lança de novo e não notifica.                                                             |
| **Jira** (API)                     | Antes de lançar, consulta os worklogs existentes no ticket. Se já há um worklog no mesmo dia com duração parecida (±5 min), pula — mesmo que tenha sido lançado manualmente. |

---

## Retry automático

Em caso de falha transitória na API do Jira (timeout, erro 5xx, rate limit), o agente tenta automaticamente até **3 vezes** antes de desistir:

| Tentativa | Aguarda     |
| --------- | ----------- |
| 1ª falha  | 5 segundos  |
| 2ª falha  | 10 segundos |
| 3ª falha  | 20 segundos |

Erros do tipo 4xx (ticket não encontrado, sem permissão) não são retentados — são erros definitivos.

---

## Health Check

A cada execução o agente atualiza o arquivo `health.json` no repositório:

```json
{
  "last_run": "2026-07-02T20:00:41",
  "status": "ok",
  "worklogs_launched": 2,
  "error": ""
}
```

Para verificar o status, acesse o arquivo diretamente no GitHub:
**Code → health.json**

| Campo               | Descrição                            |
| ------------------- | ------------------------------------ |
| `last_run`          | Data e hora da última execução (BRT) |
| `status`            | `ok` ou `error`                      |
| `worklogs_launched` | Quantidade de worklogs lançados      |
| `error`             | Mensagem de erro (vazio se ok)       |

---

## Agendamento

O agente roda automaticamente **de segunda a sexta**:

| Execução   | Horário BRT | Horário UTC | O que faz                                     |
| ---------- | ----------- | ----------- | --------------------------------------------- |
| Lembrete   | 18:00       | 21:00       | Notifica para lançar eventos no Calendar      |
| Lançamento | 20:00       | 23:00       | Lança worklogs + horas faltantes + relatórios |

> ⚠️ Durante o **horário de verão** (outubro a fevereiro) o Brasil fica em UTC-2, então os horários passam a ser 19h e 21h BRT. Ajuste os crons para `"0 22 * * 1-5"` e `"0 00 * * 1-5"` se quiser manter os horários fixos.

---

## Estrutura do projeto

```
jira-clockwork-agent/
├── .github/
│   └── workflows/
│       └── clockwork-agent.yml   # pipeline do GitHub Actions
├── src/
│   └── agent.py                  # lógica principal do agente
├── gerar_token_google.py         # script para gerar o token OAuth (roda uma vez)
├── logged_worklogs.json          # controle de eventos já processados
├── health.json                   # status da última execução
├── requirements.txt              # dependências Python
└── README.md
```

---

## Dúvidas frequentes

**O agente suporta qualquer padrão de ticket?**
Sim! O agente detecta automaticamente qualquer padrão `PROJ-XXXX` (letras maiúsculas + hífen + número). SCG-1234, CARDS-567, HPAY-890 — todos funcionam sem configuração adicional.

**E se eu esquecer de criar o evento na agenda?**
Crie o evento retroativamente na agenda com o ticket correto no título. Na próxima execução o agente detecta e lança automaticamente (respeitando o `START_DATE` e `LOOKBACK_DAYS`).

**E se eu deletar um evento já lançado?**
O agente detecta que o evento sumiu do Calendar e cancela o worklog correspondente no Jira automaticamente, notificando no Telegram.

**E se eu alterar o horário de um evento já lançado?**
O controle usa o ID do evento. Se já foi lançado, não será relançado mesmo com horário diferente. Para relançar, remova o ID do `logged_worklogs.json`.

**O token do Google expira?**
O `refresh_token` não expira. O agente renova o `access_token` automaticamente a cada execução.

**E se o Telegram ou Google Chat estiver fora?**
A notificação falha silenciosamente — o agente continua funcionando e lança os worklogs normalmente. O erro aparece apenas no log do GitHub Actions.

**Como a meta mensal é calculada?**
A meta considera apenas dias úteis do mês, excluindo finais de semana e feriados nacionais brasileiros (incluindo Carnaval, Sexta-feira Santa e Corpus Christi calculados automaticamente por ano).

**As horas faltantes consideram lançamentos manuais?**
Sim! As horas faltantes são calculadas via JQL diretamente no Jira, buscando tudo que o usuário lançou no dia — independente de ser via agente, manual ou sem evento no Calendar.

---

## Contribuindo

PRs são bem-vindos! Sugestões de melhoria:

- Suporte a múltiplos calendários
- Monitoramento externo do health check via VPS
- Notificação quando o token do Google estiver próximo de expirar

---

<p align="center">Feito com ☕ para automatizar o chato e focar no que importa.</p>
