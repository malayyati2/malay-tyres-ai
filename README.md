# Malay Tyres AI command centre

A Streamlit MVP for a human-approved multi-agent workflow for Malay Tyres:

- **Review agent**: prepares a distinct, polite reply for each Google Business Profile review.
- **Content agent**: creates campaign copy and automatically sends the image prompt to Gemini/Imagen when a key is configured.
- **Publishing agent**: routes approved assets only to the destinations selected by the operator: Google Business Profile, Instagram feed, Instagram story or WhatsApp.
- **Safety gate**: no content can be published until the operator approves it.

The current build is intentionally safe and reproducible. It uses sample reviews and an in-memory publisher adapter. Gemini/Imagen image generation is live when `GEMINI_API_KEY` is configured; Google Business Profile, Canva, Instagram and WhatsApp remain demo adapters until their production credentials and API clients are added.

## Setup

Required runtime: Python 3.11 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --all-extras
```

This creates a project-local environment from the pinned `uv.lock`. Keep real credentials out of the repository. For a future connector, use Streamlit secrets or environment variables based on `.env.example`.

### Enable automatic Gemini/Imagen images

Create `.streamlit/secrets.toml` locally (it is ignored by Git) and add:

```toml
GEMINI_API_KEY = "your-google-ai-api-key"
```

Alternatively set `GEMINI_API_KEY` in the environment. When you submit a brief in **Content studio**, the app automatically calls Imagen (`imagen-4.0-generate-001`), chooses `9:16` for Instagram story or `1:1` for other destinations, and shows the returned image for approval. No manual Gemini or Canva step is required.

## Run

```bash
uv run streamlit run app.py
```

Open the local URL printed by Streamlit. Use the sidebar to try:

1. **Reviews** → edit a suggested response, save it, then mark it as published.
2. **Content studio** → choose a topic and destinations, automatically generate the copy and image, edit, approve, then publish.
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

This image runs the same pinned Python 3.11 dependencies. Before production use, add provider OAuth/API adapters, durable storage, a scheduler for the daily review check, and provider-specific policy checks (Instagram story media dimensions, WhatsApp templates and Google reply limits).
