# Malay Tyres AI command centre

A Streamlit MVP for a human-approved multi-agent workflow for Malay Tyres:

- **Review agent**: prepares a distinct, polite reply for each Google Business Profile review.
- **Content agent**: creates campaign copy and an image prompt that can be handed to Gemini, Antigravity or Canva.
- **Publishing agent**: routes approved assets only to the destinations selected by the operator: Google Business Profile, Instagram feed, Instagram story or WhatsApp.
- **Safety gate**: no content can be published until the operator approves it.

The current build is intentionally safe and reproducible. It uses sample reviews and an in-memory publisher adapter; it does **not** make live Google, Gemini, Canva, Instagram or WhatsApp API calls. The UI shows exactly where those provider adapters and credentials belong.

## Setup

Required runtime: Python 3.11 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --all-extras
```

This creates a project-local environment from the pinned `uv.lock`. Keep real credentials out of the repository. For a future connector, use Streamlit secrets or environment variables based on `.env.example`.

## Run

```bash
uv run streamlit run app.py
```

Open the local URL printed by Streamlit. Use the sidebar to try:

1. **Reviews** → edit a suggested response, save it, then mark it as published.
2. **Content studio** → choose a topic and destinations, generate the copy and image prompt, edit, approve, then publish.
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
