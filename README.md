# Malay Tyres AI command centre

A Streamlit MVP for a human-approved multi-agent workflow for Malay Tyres:

- **Review agent**: prepares a distinct, polite reply for each Google Business Profile review.
- **Content agent**: uses Gemini for campaign copy and automatically creates a branded visual in Canva from a configured brand template.
- **Publishing agent**: routes approved assets to selected destinations where a live provider is implemented and configured.
- **Safety gate**: no content is published by the content studio until the operator approves it.

Important current limitations: the review screen starts with sample reviews until Google review sync succeeds. Google OAuth review reading/reply code is present, but Google Business Profile post publishing is not implemented. Canva brand-template autofill/export, Instagram Graph publishing, and WhatsApp Cloud image messaging require valid credentials and provider permissions. WhatsApp Business App coexistence / Embedded Signup is not implemented in this app yet. These limitations are shown explicitly rather than being presented as working connections.

## Setup

Required runtime: Python 3.11 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --all-extras
```

This creates a project-local environment from the pinned `uv.lock`. Keep real credentials out of the repository. For a future connector, use Streamlit secrets or environment variables based on `.env.example`.

### Configure Gemini, Canva, Instagram and WhatsApp

Create `.streamlit/secrets.toml` locally (it is ignored by Git) and add the following keys:

```toml
GEMINI_API_KEY = "your-google-ai-studio-api-key"

CLOUDFLARE_ACCOUNT_ID = "your-cloudflare-account-id"
CLOUDFLARE_API_TOKEN = "your-workers-ai-api-token"

CANVA_CLIENT_ID = "your-canva-client-id"
CANVA_CLIENT_SECRET = "your-canva-client-secret"
CANVA_REDIRECT_URI = "https://your-app-name.streamlit.app"
CANVA_ACCESS_TOKEN = "your-canva-oauth-access-token"
CANVA_BRAND_TEMPLATE_ID = "your-canva-brand-template-id"
CANVA_TITLE_FIELD = "TITLE"
CANVA_BODY_FIELD = "BODY"
CANVA_CTA_FIELD = "CTA"
CANVA_CTA_TEXT = "Book your tyre check today"

INSTAGRAM_ACCESS_TOKEN = "your-meta-access-token"
INSTAGRAM_BUSINESS_ACCOUNT_ID = "your-instagram-business-account-id"

WHATSAPP_ACCESS_TOKEN = "your-whatsapp-cloud-api-token"
WHATSAPP_PHONE_NUMBER_ID = "your-whatsapp-phone-number-id"
WHATSAPP_RECIPIENT_PHONE = "+91xxxxxxxxxx"
META_GRAPH_API_VERSION = "v21.0"
```

The Canva template must be a published Brand Template with autofill fields matching the configured field names. Canva's public REST APIs support brand-template autofill and PNG export, but do not expose Canva Magic Media text-to-image generation as a REST endpoint. This app therefore creates the design automatically from your branded Canva template; no manual Canva editing step is required during each campaign.

Gemini is used for campaign copy. The app attempts Canva's native image-generation REST API first, using the existing Canva OAuth token and the user's Canva AI allowance; this endpoint is currently documented by Canva as a preview API and can fail if image generation is not enabled for the Canva app or the account has no AI credits. Cloudflare Workers AI (FLUX Schnell) is an optional fallback and requires `CLOUDFLARE_ACCOUNT_ID` and `CLOUDFLARE_API_TOKEN`. Create a Gemini API key at https://aistudio.google.com/app/apikey and add it as `GEMINI_API_KEY`. If Gemini is unavailable, the app falls back to deterministic offline campaign copy.

The Connections page includes **Authorize Canva** when `CANVA_CLIENT_ID`, `CANVA_CLIENT_SECRET` and `CANVA_REDIRECT_URI` are configured. Add the exact same redirect URI in the Canva Developer Portal. After authorization, the app exchanges the OAuth code server-side using PKCE. For durable token persistence on Streamlit Cloud, configure the app's Supabase URL and service-role key; otherwise its SQLite fallback lives under `/tmp` and can be lost on restart. Never put secrets in GitHub or chat.

The Instagram image and WhatsApp image message use Canva's exported image URL. Canva export URLs expire after 24 hours, so approve and publish the campaign during that window.

## Run

```bash
uv run streamlit run app.py
```

Open the local URL printed by Streamlit. Use the sidebar to try:

1. **Reviews** → edit a suggested response, save it, then mark it as published.
2. **Content studio** → choose a topic and destinations, automatically create the copy and Canva visual, edit, approve, then publish to Instagram and/or WhatsApp.
3. **Connections** → inspect the production integration checklist.

## Test

The single project test command is:

```bash
make test
```

It runs the pure workflow tests and a headless Streamlit smoke test offline after `uv sync --all-extras` has populated the local environment.

## Deploy with Docker

```bash
docker build -t malay-tyres-ai .
docker run --rm -p 8501:8501 malay-tyres-ai
```

This image runs the same pinned Python 3.11 dependencies. Before production use, add durable storage, a scheduler for the daily review check, Canva OAuth refresh-token storage, and provider-specific policy checks (Instagram story media dimensions, WhatsApp templates and Google reply limits).

## GitHub and Streamlit Community Cloud

The repository is designed for Streamlit Community Cloud. Deploy `app.py` from the GitHub repository, then paste the same TOML keys above into the app's **Settings → Secrets** panel. Do not commit `.streamlit/secrets.toml`.

Deployment docs: https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app

Canva REST API references: https://www.canva.dev/docs/apps/rest-apis/autofill-guide/
