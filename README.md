# Jira Clockwork Agent

Lê eventos do Google Calendar com `SCG-XXXX` no título e lança worklogs automaticamente no Jira (sincronizado com o Clockwork Pro). Roda via GitHub Actions às 12h e 18h BRT, de segunda a sexta.

## Como funciona

```
GitHub Actions (12h e 18h BRT)
  → Google Calendar API  →  filtra eventos SCG-XXXX do dia
  → Jira API             →  POST /worklog com duração do evento
  → logged_worklogs.json →  evita lançar o mesmo evento duas vezes
```

## Setup — passo a passo

### 1. Fork / criar o repositório no GitHub

Crie um repositório **privado** no GitHub e suba este projeto.

### 2. Configurar Google Cloud Console

1. Acesse [console.cloud.google.com](https://console.cloud.google.com)
2. Crie um projeto (ou use um existente)
3. Ative a **Google Calendar API**: APIs & Services → Library → busque "Google Calendar API" → Enable
4. Crie credencial OAuth 2.0:
   - APIs & Services → Credentials → Create Credentials → OAuth client ID
   - Application type: **Desktop app**
   - Baixe o arquivo JSON e renomeie para `credentials.json`

### 3. Gerar token OAuth2 (uma vez no seu PC)

```bash
pip install google-auth-oauthlib
python gerar_token_google.py
```

Abrirá o browser para você autorizar o acesso ao Calendar. Após autorizar, o script exibe o JSON do token.

### 4. Criar token de API do Jira

1. Acesse [id.atlassian.com/manage-profile/security/api-tokens](https://id.atlassian.com/manage-profile/security/api-tokens)
2. Clique em **Create API token**
3. Dê um nome (ex: `clockwork-agent`) e copie o token

### 5. Configurar Secrets no GitHub

No seu repositório: **Settings → Secrets and variables → Actions → New repository secret**

| Secret | Valor |
|---|---|
| `JIRA_DOMAIN` | `sua-empresa.atlassian.net` |
| `JIRA_EMAIL` | `seu@email.com` |
| `JIRA_API_TOKEN` | token gerado no passo 4 |
| `GOOGLE_CREDENTIALS_JSON` | JSON completo gerado pelo `gerar_token_google.py` |

### 6. Commitar o `logged_worklogs.json`

O arquivo `logged_worklogs.json` (já incluso com `[]` vazio) deve estar no repositório. O agente o atualiza a cada execução para evitar duplicatas.

### 7. Testar manualmente

No GitHub: **Actions → Clockwork Agent → Run workflow**

Verifique os logs da execução para confirmar que os eventos foram encontrados e os worklogs lançados.

## Convenção dos eventos no Calendar

O título do evento deve conter `SCG-XXXX` em qualquer posição:

```
✅ SCG-2098 - [JETCARD] - Setup Noname
✅ [REUNIÃO] SCG-2050 - Alinhamento time
✅ SCG-1957
❌ Reunião geral (sem SCG → ignorado)
```

O comentário do worklog no Jira será o título do evento sem o prefixo `SCG-XXXX -`.

## Observações

- Apenas dias úteis (segunda a sexta)
- Eventos de **dia inteiro** são ignorados (só eventos com horário são lançados)
- O controle de duplicatas usa o `id` do evento do Google Calendar — se você alterar o horário do evento e ele ainda não foi lançado, funciona normalmente
- O `logged_worklogs.json` acumula IDs indefinidamente; limpe manualmente se necessário (deixar `[]`)
