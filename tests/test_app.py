from pathlib import Path

from streamlit.testing.v1 import AppTest


def test_streamlit_app_loads_without_exception():
    app = AppTest.from_file(Path(__file__).parents[1] / "app.py").run()

    assert not app.exception
    assert any("Malay Tyres command centre" in title.value for title in app.title)
    assert app.segmented_control
