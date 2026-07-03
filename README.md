# ⏱️ Jira Clockwork Agent

Agente que lê automaticamente seus eventos do **Google Calendar** e lança os worklogs no **Jira** (sincronizado com o **Clockwork Pro**), sem você precisar fazer nada.

Ao final de cada execução, você recebe notificações no **Telegram** e no **Google Chat** com o resumo do que foi lançado. Toda sexta-feira recebe o **relatório semanal**, e no último dia útil do mês o **relatório mensal** com meta x realizado.

> Criado para o time de Sustentação da RPE Processadora — mas funciona para qualquer equipe que use Jira + Clockwork Pro + Google Calendar.

---

## Como funciona

```
cron-job.org (nos horários configurados por você)
  ↓
GitHub Actions (repository_dispatch)
  ↓
Verificação preventiva de tokens (Google + Jira)
  → alerta imediato se algum token estiver inválido
  ↓
agent.py verifica a hora BRT e decide o modo:
  → 18h00–18h59 → lembrete no Telegram/Google Chat
  → 23h30–00h29 → lança worklogs + horas faltantes + relatórios
  → outros horários → encerra silenciosamente
  ↓
Jira API
  → verifica se já existe worklog (evita duplicar lançamentos manuais)
  → lança worklog com a duração exata do evento
  → retry automático (até 3x) em caso de falha transitória
  ↓
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

**Lembrete (18h):**

```
🔔 Lembrete — 02/07/2026

Não esqueça de lançar seus eventos no Google Calendar com o código do ticket!
Exemplo: SCG-2098 - [JETCARD] Setup Noname
⏰ Os worklogs serão lançados automaticamente às 23h30.
```

**Execução (23h30):**

```
⏱ Clockwork Agent — 02/07/2026 23:30

✅ Lançados:
  • SCG-2098  |  1h00  |  [JETCARD] Setup Noname

⏭ Já existiam no Jira:
  • SCG-1349  |  2026-07-02

⏱ Total lançado hoje: 1h00
⚠️ Horas faltantes: 2h00 para atingir a meta de 8h
```

**Alerta de token inválido:**

```
🔑 Clockwork Agent — Token Jira Inválido

O token de API do Jira está inválido ou foi revogado.
Gere um novo em: id.atlassian.com → Security → API tokens
Atualize o Secret JIRA_API_TOKEN no GitHub.
```

**Relatório semanal (toda sexta):**

```
📊 Resumo semanal — 30/06 a 04/07

✅ SCG-2098  |  16h00
✅ SCG-1957  |  8h00

⏱ Total semana: 24h00
📅 Dias com lançamento: 3 de 5
⚠️ Faltaram: 16h00 na semana
```

**Relatório mensal (último dia útil do mês):**

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

### 7. Criar Personal Access Token (PAT) do GitHub

O agente precisa de um PAT para commitar o `logged_worklogs.json` e `health.json`, e também para o cron-job.org disparar o workflow.

1. Acesse [github.com/settings/tokens](https://github.com/settings/tokens)
2. Clique em **Generate new token (classic)**
3. Dê um nome (ex: `clockwork-agent-push`)
4. Expiration: **No expiration**
5. Marque os escopos: ✅ **repo** e ✅ **workflow**
6. Clique em **Generate token** e copie o token gerado

> ⚠️ Guarde o token gerado — ele só é exibido uma vez.

---

### 8. Configurar Secrets no GitHub

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
| `GH_PAT`                  | Personal Access Token gerado no passo 7             | `ghp_xxxxxxxx`                    |
| `START_DATE`              | Data de corte — eventos anteriores são ignorados    | `2026-07-01`                      |
| `LOOKBACK_DAYS`           | Dias para trás na busca (usado se START_DATE vazio) | `7`                               |
| `DAILY_HOURS_GOAL`        | Meta diária de horas                                | `8`                               |
| `FORCE_MODE`              | `launch` ou `reminder` para forçar modo manualmente | _(vazio)_                         |
| `FORCE_MONTHLY`           | `true` para forçar relatório mensal                 | _(vazio)_                         |

> **Dica:** `START_DATE` deve ser o dia em que você começou a usar o agente, para evitar duplicar lançamentos manuais anteriores.

> ⚠️ Sempre volte `FORCE_MODE` e `FORCE_MONTHLY` para vazio após os testes.

---

### 9. Configurar o cron-job.org

O **cron-job.org** é o serviço gratuito que dispara o workflow no horário exato, resolvendo o problema de atraso do GitHub Actions.

> Por que não usar o cron nativo do GitHub Actions? O GitHub não garante execução no horário exato — pode atrasar horas em períodos de pico. O cron-job.org dispara o workflow via API do GitHub com precisão de segundos.

**Criando a conta:**

1. Acesse [cron-job.org](https://cron-job.org) e crie uma conta gratuita

**Criando o cron job de lembrete (18h):**

1. Clique em **Create cronjob**
2. Preencha:
   - **Title:** `Clockwork Agent — Lembrete`
   - **URL:** `https://api.github.com/repos/SEU_USUARIO/jira-clockwork-agent/dispatches`
   - **Execution schedule:** clique em **Custom** e digite no campo Crontab:
     ```
     0 21 * * 1-5
     ```
     _(18h BRT = 21h UTC, segunda a sexta)_
3. Clique na aba **ADVANCED** e configure:
   - **Request method:** `POST`
   - **Request headers:** clique em **Add header** e adicione:
     - `Authorization` → `Bearer SEU_GH_PAT`
     - `Content-Type` → `application/json`
   - **Request body:**
     ```json
     { "event_type": "clockwork" }
     ```
4. Clique em **Create** para salvar

**Criando o cron job de lançamento (23h30):**

Repita o processo acima com:

- **Title:** `Clockwork Agent — Lançamento`
- **Crontab expression:**
  ```
  30 2 * * 2-6
  ```
  _(23h30 BRT = 02h30 UTC do dia seguinte, terça a sábado em UTC)_
- Os demais campos são idênticos ao job de lembrete

**Verificando se está funcionando:**

No histórico do cron-job.org, cada execução deve mostrar **Status: 204 No Content** — isso significa que o GitHub recebeu o disparo com sucesso.

No GitHub Actions, os runs disparados pelo cron-job.org aparecem como:

```
Repository dispatch triggered by SEU_USUARIO
```

> **Dica:** você pode ajustar os horários para o que preferir — o `agent.py` decide automaticamente o que fazer baseado na hora BRT:
>
> - **18h00–18h59** → envia o lembrete
> - **23h30–00h29** → lança os worklogs
> - **outros horários** → encerra silenciosamente (sem fazer nada)

---

### 10. Testar manualmente

No GitHub: **Actions → Clockwork Agent → Run workflow**

Para forçar um modo específico fora do horário, defina temporariamente no Secret `FORCE_MODE`:

- `launch` → força o lançamento de worklogs
- `reminder` → força o envio do lembrete

Exemplo de log esperado:

```
=== Jira Clockwork Agent iniciado === (23:31 BRT)
Modo: launch
Verificando tokens...
✅ Token Google Calendar válido
✅ Token Jira válido — usuário: michael.aguiar
Data de corte (START_DATE): 2026-07-01
Buscando eventos de 2026-07-01T00:00:00-03:00 até 2026-07-02T23:31:00-03:00
7 evento(s) encontrado(s) no período.
3 evento(s) com ticket no título.
✅ Worklog lançado: SCG-2050 | 3600s | 'Alinhamento time'
Total lançado no Jira em 2026-07-02: 28800s (8h)
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

## Verificação preventiva de tokens

No início de **cada execução**, o agente verifica se os tokens do Google e do Jira ainda são válidos — antes de tentar qualquer operação.

| Token               | O que verifica                                     | Alerta enviado quando                                                |
| ------------------- | -------------------------------------------------- | -------------------------------------------------------------------- |
| **Google Calendar** | Tenta renovar o `access_token` via `refresh_token` | O `refresh_token` foi revogado (ex: troca de senha, acesso removido) |
| **Jira API**        | Chama `GET /rest/api/3/myself`                     | Retorna 401 — token inválido ou revogado                             |

Se qualquer token estiver inválido, o agente **para imediatamente**, envia um alerta no Telegram/Google Chat com instruções de correção e salva `status=error` no `health.json`.

**Como corrigir o token do Google revogado:**

1. Execute `python gerar_token_google.py` no seu PC
2. Atualize o Secret `GOOGLE_CREDENTIALS_JSON` no GitHub

**Como corrigir o token do Jira inválido:**

1. Acesse `id.atlassian.com → Security → API tokens`
2. Gere um novo token
3. Atualize o Secret `JIRA_API_TOKEN` no GitHub

---

## Retry automático

Em caso de falha transitória na API do Jira (timeout, erro 5xx, rate limit), o agente tenta automaticamente até **3 vezes** antes de desistir:

| Tentativa | Aguarda     |
| --------- | ----------- |
| 1ª falha  | 5 segundos  |
| 2ª falha  | 10 segundos |
| 3ª falha  | 20 segundos |

---

## Health Check

A cada execução o agente atualiza o arquivo `health.json` no repositório:

```json
{
  "last_run": "2026-07-02T23:31:00",
  "status": "ok",
  "worklogs_launched": 2,
  "error": ""
}
```

Para verificar o status, acesse o arquivo diretamente no GitHub: **Code → health.json**

| Campo               | Descrição                            |
| ------------------- | ------------------------------------ |
| `last_run`          | Data e hora da última execução (BRT) |
| `status`            | `ok` ou `error`                      |
| `worklogs_launched` | Quantidade de worklogs lançados      |
| `error`             | Mensagem de erro (vazio se ok)       |

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
O `refresh_token` não expira normalmente. Mas pode ser revogado se você trocar a senha do Google ou remover o acesso do app. O agente detecta e avisa no Telegram com instruções de correção.

**E se o Telegram ou Google Chat estiver fora?**
A notificação falha silenciosamente — o agente continua funcionando e lança os worklogs normalmente.

**Como a meta mensal é calculada?**
A meta considera apenas dias úteis do mês, excluindo finais de semana e feriados nacionais brasileiros (incluindo Carnaval, Sexta-feira Santa e Corpus Christi calculados automaticamente por ano).

**As horas faltantes consideram lançamentos manuais?**
Sim! As horas faltantes são calculadas via JQL diretamente no Jira, buscando tudo que o usuário lançou no dia — independente de ser via agente, manual ou sem evento no Calendar.

**Por que usar o cron-job.org em vez do cron nativo do GitHub Actions?**
O GitHub Actions não garante execução no horário exato do cron — pode atrasar horas em períodos de pico. O cron-job.org dispara o workflow via API do GitHub com precisão de segundos, garantindo que o lembrete e o lançamento aconteçam nos horários configurados.

**Por que preciso do GH_PAT?**
O GitHub Actions usa por padrão o `GITHUB_TOKEN` que não consegue fazer push em branches protegidas nem disparar workflows via API externa. O PAT é usado tanto para o agente commitar os arquivos de controle quanto para o cron-job.org disparar o workflow.

**Posso mudar os horários do lembrete e do lançamento?**
Sim! Basta ajustar os crons no cron-job.org. O `agent.py` aceita qualquer horário dentro das janelas:

- **Lembrete:** qualquer horário entre 18h00 e 18h59 BRT
- **Lançamento:** qualquer horário entre 23h30 e 00h29 BRT

---

## Contribuindo

PRs são bem-vindos! Sugestões de melhoria:

- Suporte a múltiplos calendários
- Monitoramento externo do health check

---

<p align="center">Feito com ☕ para automatizar o chato e focar no que importa.</p>
