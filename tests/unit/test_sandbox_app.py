from __future__ import annotations

import json

from services.app.main import load_runtime_flags


def test_load_runtime_flags_defaults_missing_file(tmp_path):
    flags = load_runtime_flags(tmp_path / "missing.json")

    assert flags == {"crash_on_start": False}


def test_load_runtime_flags_reads_crash_toggle(tmp_path):
    flags_path = tmp_path / "flags.json"
    flags_path.write_text(json.dumps({"crash_on_start": True}), encoding="utf-8")

    flags = load_runtime_flags(flags_path)

    assert flags["crash_on_start"] is True
