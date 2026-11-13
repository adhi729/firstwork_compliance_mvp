from pathlib import Path


def test_engine_source_does_not_import_django():
    engine_dir = Path(__file__).parents[2] / "domain" / "engine"
    for source_file in engine_dir.rglob("*.py"):
        source = source_file.read_text()
        assert "import django" not in source
        assert "from django" not in source
