# Malay Tyres AI command centre

A Streamlit MVP for a human-approved multi-agent workflow for Malay Tyres:

- **Review agent**: prepares a distinct, polite reply for each Google Business Profile review without using the customer name; you can choose the salutation and regenerate a reply.
- **Content agent**: prepares campaign copy and a fresh full-poster Canva AI prompt for manual use on canva.com.
- **Publishing agent**: routes approved assets to selected destinations where a live provider is implemented and configured.
- **Safety gate**: no content is published by the content studio until the operator approves it.

Important current limitations: the review screen starts with sample reviews until Google review sync succeeds. Google OAuth review reading/reply code is present, but Google Business Profile post publishing is not implemented. Canva poster creation is manual: the app creates prompts, you create and inspect the poster on canva.com, and then paste its public image/export URL back into the app. Instagram Graph publishing and WhatsApp Cloud image messaging require valid credentials and provider permissions. WhatsApp Business App coexistence / Embedded Signup is not implemented in this app yet. These limitations are shown explicitly rather than being presented as working connections.

## Setup

Required runtime: Python 3.11 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --all-extras
```

This creates a project-local environment from the pinned `uv.lock`. Keep real credentials out of the repository. For a future connector, use Streamlit secrets or environment variables based on `.env.example`.

### Configure campaign copy, Instagram and WhatsApp

Create `.streamlit/secrets.toml` locally (it is ignored by Git) and add the following keys:

```toml
GEMINI_API_KEY = "your-google-ai-studio-api-key"

CANVA_CLIENT_ID = "your-canva-client-id"
CANVA_CLIENT_SECRET = "your-canva-client-secret"
CANVA_REDIRECT_URI = "https://your-app-name.streamlit.app"
CANVA_ACCESS_TOKEN = "your-canva-oauth-access-token"
BUSINESS_NAME = "Malay Tyres"
BUSINESS_ADDRESS = "74, Dewas Road, Ujjain (M.P.) 456010"
BUSINESS_PHONE = "+91 99260 91313"

INSTAGRAM_ACCESS_TOKEN = "your-meta-access-token"
INSTAGRAM_BUSINESS_ACCOUNT_ID = "your-instagram-business-account-id"

WHATSAPP_ACCESS_TOKEN = "your-whatsapp-cloud-api-token"
WHATSAPP_PHONE_NUMBER_ID = "your-whatsapp-phone-number-id"
WHATSAPP_RECIPIENT_PHONE = "+91xxxxxxxxxx"
META_GRAPH_API_VERSION = "v21.0"
```

The app does not call Canva image-generation APIs. In **Content studio**, it creates a new full-poster prompt every time. Copy that prompt into Canva's **AI Poster Generator / Create posters with AI**—not the image-only generator—so Canva creates the photograph, layout, and text in one poster. Inspect the generated poster, replace any mistyped logo or text with the real Malay Tyres assets, export the finished poster, and paste a public HTTPS Canva image/export URL back into the app. Add a short description of what the final poster shows; the app uses it to prepare a matching caption. Publishing remains approval-gated.

The generated Canva prompt includes a new visual direction and layout direction on every request. The prompt asks Canva AI for a complete poster, not a fixed Brand Template autofill.

Gemini is optional for campaign copy. If Gemini is unavailable or its quota is exhausted, the app uses a deterministic caption fallback and still creates a fresh manual Canva AI prompt.

The Connections page includes **Authorize Canva** when `CANVA_CLIENT_ID`, `CANVA_CLIENT_SECRET` and `CANVA_REDIRECT_URI` are configured. Add the exact same redirect URI in the Canva Developer Portal. After authorization, the app exchanges the OAuth code server-side using PKCE. For durable token persistence on Streamlit Cloud, configure the app's Supabase URL and service-role key; otherwise its SQLite fallback lives under `/tmp` and can be lost on restart. Never put secrets in GitHub or chat.

Instagram and WhatsApp publishing use the public Canva image/export URL that you paste into the app. The URL must remain reachable while Meta fetches the image; approve and publish during its temporary URL window.

## Run

```bash
uv run streamlit run app.py
```

Open the local URL printed by Streamlit. Use the sidebar to try:

1. **Reviews** → choose Dear Sir, Dear Ma'am or Dear Customer, regenerate a different reply when needed, edit it, save it, then publish it to Google.
2. **Content studio** → create a caption and fresh full-poster Canva AI prompt, copy it into Canva manually, paste the public poster URL and image description back into the app, edit, approve, then publish to Instagram and/or WhatsApp.
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

Canva manual workflow: paste the generated prompt into Canva AI, inspect the finished poster, correct any AI-mistyped logo or text, export it, and paste a public HTTPS image/export URL into Content studio.
