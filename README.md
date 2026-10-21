# Malay Tyres AI command centre

A Streamlit MVP for a human-approved multi-agent workflow for Malay Tyres:

- **Review agent**: prepares a distinct, polite reply for each Google Business Profile review without using the customer name; you can choose the salutation and regenerate a reply.
- **Content agent**: uses Gemini for campaign copy and image generation, and Canva for a second independent image generation so you can compare and select.
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
GEMINI_IMAGE_MODEL = "gemini-3.1-flash-image"

CLOUDFLARE_ACCOUNT_ID = "your-cloudflare-account-id"
CLOUDFLARE_API_TOKEN = "your-workers-ai-api-token"

CANVA_CLIENT_ID = "your-canva-client-id"
CANVA_CLIENT_SECRET = "your-canva-client-secret"
CANVA_REDIRECT_URI = "https://your-app-name.streamlit.app"
CANVA_ACCESS_TOKEN = "your-canva-oauth-access-token"
CANVA_IMAGE_MODEL = ""
CANVA_BRAND_TEMPLATE_ID = "your-canva-brand-template-id"
CANVA_TITLE_FIELD = "TITLE"
CANVA_BODY_FIELD = "BODY"
CANVA_CTA_FIELD = "CTA"
CANVA_IMAGE_FIELD = "IMAGE"
CANVA_CTA_TEXT = "Book your tyre check today"

INSTAGRAM_ACCESS_TOKEN = "your-meta-access-token"
INSTAGRAM_BUSINESS_ACCOUNT_ID = "your-instagram-business-account-id"

WHATSAPP_ACCESS_TOKEN = "your-whatsapp-cloud-api-token"
WHATSAPP_PHONE_NUMBER_ID = "your-whatsapp-phone-number-id"
WHATSAPP_RECIPIENT_PHONE = "+91xxxxxxxxxx"
META_GRAPH_API_VERSION = "v21.0"
```

The Canva template must be a published Brand Template with autofill fields matching the configured field names, including `IMAGE` when you want the generated image inserted into the branded design. Canva AI image generation is a preview API, consumes Canva AI credits, requires the `asset:write` scope, and returns a temporary image/export URL.

The Canva overlay text is automatically shortened before autofill so it stays within the template-safe limits: `TITLE` is 45 characters or fewer, `BODY` is 140 characters or fewer, and `CTA` is 20 characters or fewer. The full caption remains available for social publishing; only the text rendered on the image is shortened.

Gemini is used for both campaign copy and image generation. Canva creates an independent second image, and both options are shown for comparison. Create a Gemini API key at https://aistudio.google.com/app/apikey and add it as `GEMINI_API_KEY`. If Gemini is unavailable, the app keeps the deterministic caption fallback and reports the image-provider error instead of claiming an image was created.

The Connections page includes **Authorize Canva** when `CANVA_CLIENT_ID`, `CANVA_CLIENT_SECRET` and `CANVA_REDIRECT_URI` are configured. Add the exact same redirect URI in the Canva Developer Portal. After authorization, the app exchanges the OAuth code server-side using PKCE. For durable token persistence on Streamlit Cloud, configure the app's Supabase URL and service-role key; otherwise its SQLite fallback lives under `/tmp` and can be lost on restart. Never put secrets in GitHub or chat.

If a Gemini image is selected and a Canva Brand Template is configured, the Gemini image is uploaded into Canva and exported through the template. Social publishing uses the selected public Canva export URL; approve and publish during its temporary URL window.

## Run

```bash
uv run streamlit run app.py
```

Open the local URL printed by Streamlit. Use the sidebar to try:

1. **Reviews** → choose Dear Sir, Dear Ma'am or Dear Customer, regenerate a different reply when needed, edit it, save it, then publish it to Google.
2. **Content studio** → create the caption plus Gemini and Canva images, select the better option, edit, approve, then publish to Instagram and/or WhatsApp.
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
