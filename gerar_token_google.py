"""
Execute este script UMA VEZ no seu PC para gerar o token OAuth2 do Google.
O JSON gerado vai para o Secret GOOGLE_CREDENTIALS_JSON no GitHub.

Pré-requisitos:
  pip install google-auth-oauthlib
  - Baixe o credentials.json do Google Cloud Console
    (APIs & Services > Credentials > OAuth 2.0 Client ID > Download JSON)
"""

import json
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]

flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
creds = flow.run_local_server(port=0)

token_data = {
    "token":         creds.token,
    "refresh_token": creds.refresh_token,
    "token_uri":     creds.token_uri,
    "client_id":     creds.client_id,
    "client_secret": creds.client_secret,
}

output = json.dumps(token_data, indent=2)
print("\n=== Cole este JSON no Secret GOOGLE_CREDENTIALS_JSON do GitHub ===\n")
print(output)

with open("google_token.json", "w") as f:
    f.write(output)

print("\n✅ Também salvo em google_token.json (não commite este arquivo!)")
