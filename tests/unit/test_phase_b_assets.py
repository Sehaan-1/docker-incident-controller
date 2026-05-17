from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_known_good_runtime_flags_start_safe():
    flags = json.loads((ROOT / "services/app/runtime/flags.json").read_text(encoding="utf-8"))

    assert flags == {"crash_on_start": False}


def test_nginx_site_conf_proxies_health_to_app():
    site_conf = (ROOT / "services/nginx/site.conf").read_text(encoding="utf-8")

    assert "proxy_pass http://app:8000/health;" in site_conf
    assert "listen 80 default_server;" in site_conf


def test_fault_injection_scripts_are_break_only():
    nginx_break = (ROOT / "fault_injection/break_nginx_config.sh").read_text(encoding="utf-8")
    app_break = (ROOT / "fault_injection/enable_app_crash.sh").read_text(encoding="utf-8")
    nginx_break_ps = (ROOT / "fault_injection/break_nginx_config.ps1").read_text(encoding="utf-8")
    app_break_ps = (ROOT / "fault_injection/enable_app_crash.ps1").read_text(encoding="utf-8")

    assert "definitely_invalid_directive" in nginx_break
    assert "definitely_invalid_directive" in nginx_break_ps
    assert "crash_on_start" in app_break
    assert "crash_on_start" in app_break_ps
    assert "true" in app_break
    assert "true" in app_break_ps
    assert 'crash_on_start": false' not in app_break
    assert 'crash_on_start": false' not in app_break_ps
