.PHONY: test run

test:
	uv run --frozen --offline --extra dev pytest -q

run:
	uv run --frozen streamlit run app.py
