#!/usr/bin/env python3
"""gen-supabase-env.py <app> <api_domain> <site_url>  → vypíše kompletní .env
Generuje všechny secrety pro per-app self-hosted Supabase stack (HS256 JWT jako Freio).
"""
import sys, secrets, hmac, hashlib, base64, json, time

app, apidom, site = sys.argv[1], sys.argv[2], sys.argv[3]

def rand_hex(n): return secrets.token_hex(n)
def rand_b64(n): return base64.b64encode(secrets.token_bytes(n)).decode().rstrip("=")

JWT_SECRET = rand_hex(32)                      # 64 hex znaků
def jwt(role):
    now = int(time.time())
    hdr = base64.urlsafe_b64encode(json.dumps({"alg":"HS256","typ":"JWT"}).encode()).decode().rstrip("=")
    pld = base64.urlsafe_b64encode(json.dumps({"role":role,"iss":"supabase","iat":now,"exp":now+60*60*24*365*10}).encode()).decode().rstrip("=")
    msg = f"{hdr}.{pld}"
    sig = base64.urlsafe_b64encode(hmac.new(JWT_SECRET.encode(), msg.encode(), hashlib.sha256).digest()).decode().rstrip("=")
    return f"{msg}.{sig}"

ANON = jwt("anon"); SERVICE = jwt("service_role")
DASH_PW = rand_b64(18)
REALTIME_KEY = rand_hex(8)   # PŘESNĚ 16 znaků

env = f"""# self-hosted Supabase — {app} (generováno, secrety, chmod 600, NIKDY do gitu)
POSTGRES_PASSWORD={rand_hex(24)}
POSTGRES_HOST=db
POSTGRES_DB=postgres
POSTGRES_PORT=5432
JWT_SECRET={JWT_SECRET}
JWT_EXPIRY=3600
ANON_KEY={ANON}
SERVICE_ROLE_KEY={SERVICE}
SUPABASE_PUBLISHABLE_KEY={ANON}
SUPABASE_SECRET_KEY={SERVICE}
JWT_KEYS=
JWT_JWKS=
ANON_KEY_ASYMMETRIC=
SERVICE_ROLE_KEY_ASYMMETRIC=
SECRET_KEY_BASE={rand_hex(32)}
VAULT_ENC_KEY={rand_hex(16)}
PG_META_CRYPTO_KEY={rand_hex(32)}
REALTIME_DB_ENC_KEY={REALTIME_KEY}
DASHBOARD_USERNAME=admin
DASHBOARD_PASSWORD={DASH_PW}
STUDIO_DEFAULT_ORGANIZATION={app}
STUDIO_DEFAULT_PROJECT={app}
SUPABASE_PUBLIC_URL=https://{apidom}
API_EXTERNAL_URL=https://{apidom}
SITE_URL={site}
ADDITIONAL_REDIRECT_URLS={site},{site}/**,{site}/auth/callback
DISABLE_SIGNUP=false
ENABLE_EMAIL_SIGNUP=true
ENABLE_EMAIL_AUTOCONFIRM=true
ENABLE_ANONYMOUS_USERS=false
ENABLE_PHONE_SIGNUP=false
ENABLE_PHONE_AUTOCONFIRM=false
MAILER_URLPATHS_CONFIRMATION=/auth/v1/verify
MAILER_URLPATHS_INVITE=/auth/v1/verify
MAILER_URLPATHS_RECOVERY=/auth/v1/verify
MAILER_URLPATHS_EMAIL_CHANGE=/auth/v1/verify
SMTP_ADMIN_EMAIL=noreply@{apidom.split('.',1)[1] if '.' in apidom else apidom}
SMTP_HOST=
SMTP_PORT=587
SMTP_USER=
SMTP_PASS=
SMTP_SENDER_NAME={app}
KONG_HTTP_PORT=8000
KONG_HTTPS_PORT=8443
PGRST_DB_SCHEMAS=public,storage,graphql_public
PGRST_DB_MAX_ROWS=1000
PGRST_DB_EXTRA_SEARCH_PATH=public,extensions
STORAGE_BACKEND=file
STORAGE_S3_BUCKET=
STORAGE_S3_ENDPOINT=
STORAGE_S3_REGION=auto
STORAGE_S3_ACCESS_KEY_ID=
STORAGE_S3_SECRET_ACCESS_KEY=
FUNCTIONS_VERIFY_JWT=false
OPENAI_API_KEY=
POOLER_TENANT_ID={app}
POOLER_DEFAULT_POOL_SIZE=20
POOLER_MAX_CLIENT_CONN=100
POOLER_PROXY_PORT_TRANSACTION=6543
"""
print(env)
"""ANON/SERVICE keys se vypíšou do stderr pro app konfiguraci."""
sys.stderr.write(f"ANON_KEY={ANON}\nSERVICE_ROLE_KEY={SERVICE}\nDASHBOARD_PASSWORD={DASH_PW}\n")
