FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

COPY pyproject.toml uv.lock .python-version ./
RUN pip install --no-cache-dir uv==0.12.23 \
    && uv sync --frozen --no-dev

COPY app.py ./
COPY tyre_agents ./tyre_agents
COPY .streamlit ./.streamlit

EXPOSE 8501
CMD ["uv", "run", "--frozen", "streamlit", "run", "app.py", "--server.address=0.0.0.0"]
