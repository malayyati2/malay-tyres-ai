# Malay Tyres AI command centre

A Streamlit MVP for a human-approved multi-agent workflow for Malay Tyres:

- **Review agent**: prepares a distinct, polite reply for each Google Business Profile review.
- **Content agent**: creates campaign copy and automatically creates a branded visual in Canva from a configured brand template.
- **Publishing agent**: routes approved assets only to the destinations selected by the operator: Google Business Profile, Instagram feed, Instagram story or WhatsApp.
- **Safety gate**: no content can be published until the operator approves it.

The current build is intentionally safe and reproducible. It uses sample reviews. Canva brand-template autofill/export, Instagram Graph publishing and WhatsApp Cloud image messaging are wired as provider adapters; they become live only when the required secrets are configured. Google Business Profile remains a safe demo adapter in this trial.

## Setup

Required runtime: Python 3.11 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --all-extras
```

This creates a project-local environment from the pinned `uv.lock`. Keep real credentials out of the repository. For a future connector, use Streamlit secrets or environment variables based on `.env.example`.

### Configure Canva, Instagram and WhatsApp

Create `.streamlit/secrets.toml` locally (it is ignored by Git) and add the following keys:

```toml
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
