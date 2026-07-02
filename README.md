# ⏱️ Jira Clockwork Agent

Agente que lê automaticamente seus eventos do **Google Calendar** e lança os worklogs no **Jira** (sincronizado com o **Clockwork Pro**), sem você precisar fazer nada.

Ao final de cada execução, você recebe uma notificação no **Telegram** com o resumo do que foi lançado. Toda sexta-feira às 18h, recebe também o **relatório semanal** consolidado.

> Criado para o time de Sustentação da RPE Processadora — mas funciona para qualquer equipe que use Jira + Clockwork Pro + Google Calendar.

---

## Como funciona

```
GitHub Actions (12h e 18h, seg–sex)
  ↓
Google Calendar API
  → busca eventos do período configurado
  → filtra os que têm SCG-XXXX no título
  ↓
Jira API
  → verifica se já existe worklog no ticket (evita duplicar lançamentos manuais)
  → lança worklog com a duração exata do evento
  ↓
Telegram
  → notificação com resumo da execução
  → relatório semanal toda sexta às 18h
  ↓
logged_worklogs.json
  → registra os eventos já processados (evita lançar duas vezes)
```

---

## Convenção dos eventos no Calendar

O título do evento deve conter o código do ticket (`SCG-XXXX`) em qualquer posição:

```
✅ SCG-2098 - [JETCARD] - Setup Noname
✅ SCG-1957 - Avenida PCJ Refinanciamento
✅ [REUNIÃO] SCG-2050 - Alinhamento time
❌ Daily Sustentação          ← sem SCG, será ignorado
❌ Almoço                     ← sem SCG, será ignorado
```

O comentário do worklog no Jira será o título do evento **sem o prefixo `SCG-XXXX -`**.

> ⚠️ Eventos de **dia inteiro** são ignorados. Apenas eventos com horário definido (início e fim) são processados.

---

## Notificações no Telegram

**Execução diária** (12h e 18h):

```
⏱ Clockwork Agent — 02/07/2026 18:00

✅ Lançados:
  • SCG-2098  |  1h00  |  [JETCARD] Setup Noname

⏭ Já existiam no Jira:
  • SCG-1349  |  2026-07-02

⏱ Total lançado: 1h00
```

**Relatório semanal** (toda sexta às 18h):

```
📊 Resumo semanal — 30/06 a 04/07

✅ SCG-2098  |  3h00  |  [JETCARD] Setup Noname
✅ SCG-1957  |  1h30  |  Avenida PCJ Refinanciamento
✅ SCG-2050  |  2h00  |  Alinhamento time

⏱ Total semana: 6h30
📅 Dias com lançamento: 3 de 5
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

1. Mande qualquer mensagem para o seu bot recém-criado no Telegram
2. Acesse no browser (substituindo `SEU_TOKEN` pelo token recebido):

```
https://api.telegram.org/botSEU_TOKEN/getUpdates
```

3. No JSON retornado, copie o número do campo `"id"` dentro de `"chat"`:

```json
"chat": { "id": 123456789, ... }
```

---

### 3. Ativar a Google Calendar API

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

### 4. Gerar o token OAuth do Google (uma vez no seu PC)

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

### 5. Criar token de API do Jira

1. Acesse [id.atlassian.com/manage-profile/security/api-tokens](https://id.atlassian.com/manage-profile/security/api-tokens)
2. Clique em **Create API token**
3. Dê um nome (ex: `clockwork-agent`) e copie o token gerado

---

### 6. Configurar Secrets no GitHub

No repositório: **Settings → Secrets and variables → Actions → New repository secret**

| Secret                    | Valor                              | Exemplo                    |
| ------------------------- | ---------------------------------- | -------------------------- |
| `JIRA_DOMAIN`             | Domínio do seu Jira (sem https://) | `suaempresa.atlassian.net` |
| `JIRA_EMAIL`              | E-mail da sua conta Atlassian      | `voce@empresa.com`         |
| `JIRA_API_TOKEN`          | Token gerado no passo 5            | `ATATxxxxxxxx`             |
| `GOOGLE_CREDENTIALS_JSON` | Conteúdo do `google_token.json`    | `{"token": "...", ...}`    |
| `TELEGRAM_BOT_TOKEN`      | Token do bot criado no passo 2     | `123456:ABCdef...`         |
| `TELEGRAM_CHAT_ID`        | ID do seu chat com o bot           | `388676023`                |

---

### 7. Ajustar as variáveis no workflow

Abra `.github/workflows/clockwork-agent.yml` e configure:

```yaml
START_DATE: "2026-07-01" # data de corte — eventos anteriores são ignorados
LOOKBACK_DAYS: "7" # dias para trás (usado se START_DATE não estiver definido)
```

> **Dica:** defina `START_DATE` como o dia em que começou a usar o agente. Isso garante que lançamentos manuais anteriores não sejam duplicados.

---

### 8. Testar manualmente

No GitHub: **Actions → Clockwork Agent → Run workflow**

Verifique o log do step **"Rodar agente"** e confira se chegou mensagem no Telegram.

Exemplo de log esperado:

```
=== Jira Clockwork Agent iniciado ===
Data de corte (START_DATE): 2026-07-01
Buscando eventos de 2026-07-01T00:00:00-03:00 até 2026-07-02T18:00:00-03:00
7 evento(s) encontrado(s) no período.
3 evento(s) com SCG no título.
⏭️  Já lançado pelo agente: SCG-2098 (abc12345)
⚠️  Worklog já existe no Jira: SCG-1957 | 2026-07-01 | 5400s
✅ Worklog lançado: SCG-2050 | 3600s | 'Alinhamento time'
=== Concluído: 1 worklog(s) lançado(s) ===
```

---

## Proteções contra duplicata

O agente tem **duas camadas** de proteção:

| Camada                             | Como funciona                                                                                                                                                                                                                              |
| ---------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **Local** (`logged_worklogs.json`) | Registra o ID de cada evento do Calendar já processado. Se o agente já lançou, não lança de novo e não notifica.                                                                                                                           |
| **Jira** (API)                     | Antes de lançar, consulta os worklogs existentes no ticket. Se já há um worklog no mesmo dia com duração parecida (±5 min), pula — mesmo que tenha sido lançado manualmente. Esses são notificados no Telegram como "Já existiam no Jira". |

---

## Configurações disponíveis

Todas configuradas diretamente no `.github/workflows/clockwork-agent.yml`:

| Variável        | Padrão    | Descrição                                                                    |
| --------------- | --------- | ---------------------------------------------------------------------------- |
| `START_DATE`    | _(vazio)_ | Data de corte no formato `YYYY-MM-DD`. Eventos anteriores são ignorados.     |
| `LOOKBACK_DAYS` | `7`       | Quantos dias para trás buscar (usado quando `START_DATE` não está definido). |

---

## Agendamento

O agente roda automaticamente **de segunda a sexta** em dois horários:

| Execução     | Horário BRT | Horário UTC |
| ------------ | ----------- | ----------- |
| Meio-dia     | 12:00       | 15:00       |
| Final do dia | 18:00       | 21:00       |

> ⚠️ Durante o **horário de verão** (outubro a fevereiro) o Brasil fica em UTC-2, então o agente passa a rodar às 13h e 19h BRT. Ajuste o cron para `"0 14 * * 1-5"` e `"0 20 * * 1-5"` se quiser manter os horários fixos.

Você também pode rodar manualmente a qualquer momento via **Actions → Run workflow**.

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
├── requirements.txt              # dependências Python
└── README.md
```

---

## Dúvidas frequentes

**O agente vai lançar horas em eventos de reunião ou apenas de trabalho?**
Todos os eventos com `SCG-XXXX` no título serão processados. Você controla o que o agente lança pelo título dos seus eventos no Calendar.

**E se eu esquecer de criar o evento na agenda?**
Crie o evento retroativamente na agenda com o ticket correto no título. Na próxima execução o agente detecta e lança automaticamente (respeitando o `START_DATE` e `LOOKBACK_DAYS`).

**E se eu alterar o horário de um evento já lançado?**
O controle de duplicatas usa o ID do evento do Calendar. Se o evento já foi lançado pelo agente, **não será lançado novamente** mesmo que o horário mude. Para relançar, remova o ID correspondente do `logged_worklogs.json`.

**O token do Google expira?**
O `refresh_token` não expira. O agente renova o `access_token` automaticamente a cada execução.

**E se o Telegram estiver fora?**
A notificação falha silenciosamente — o agente continua funcionando e lança os worklogs normalmente. O erro aparece apenas no log do GitHub Actions.

**Posso usar com outro padrão de ticket além de SCG-XXXX?**
Sim! Edite a linha `SCG_PATTERN` no `src/agent.py`:

```python
# Exemplo para suportar qualquer padrão PROJ-XXXX
SCG_PATTERN = re.compile(r"\b([A-Z]+-\d+)\b")
```

---

## Contribuindo

PRs são bem-vindos! Sugestões de melhoria:

- Suporte a múltiplos calendários
- Suporte a outros padrões de ticket além de `SCG-XXXX`
- Notificação quando o token do Google estiver próximo de expirar

---

<p align="center">Feito com ☕ para automatizar o chato e focar no que importa.</p>
