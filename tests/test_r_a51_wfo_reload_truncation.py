"""Tests fuer R-A51 — WFO-Gist-Reload Truncation-Fix.

Bug-Anlass: Fr 29.05.2026 14:00 CEST. Carlos triggerte WFO-Re-Run heute
(13:33 UTC, success). GitHub-Action pushte korrekt in den Gist
(_wfo_meta.json last_wfo_push=29.05). Aber das Dashboard zeigte weiterhin
den ALTEN Run vom 24.05.

Diagnose: check_and_reload_wfo_output() in persistence.py las roh
f.get("content") statt den gemeinsamen Helper _fetch_gist_file_content()
zu nutzen (wie ALLE anderen Watchdogs: optimizer, backtest, ml_training).

GitHub-Gist-API liefert bei vielen/grossen Files (dieser Gist hat 20+):
  - truncated: True
  - content: "" (leer inline)
  - raw_url: vorhanden (echte Daten dort)

wfo_status.json: size=3307, truncated=True, inline-content=LEER.

Folge-Bug-Kette:
  1. "if f and f.get(content)" war falsy (content leer) → Reload skip
  2. save_json(WFO_LAST_APPLIED_FILE) lief UNBEDINGT danach → Marker
     auf "applied" obwohl wfo_status.json nie geladen
  3. Naechster Lauf: last_applied == gist_push → "bereits angewendet"
     → nie Retry → Dashboard permanent stale (wochenlang)

R-A51 Fix:
  1. _fetch_gist_file_content() nutzen (raw_url-Fallback bei truncated)
  2. Marker NUR setzen wenn reloaded_count > 0 (sonst Retry naechster Lauf)
"""

import json
from unittest.mock import patch, MagicMock


# ---------------------------------------------------------------------------
# Source-Based-Regression
# ---------------------------------------------------------------------------

def test_r_a51_uses_fetch_gist_helper():
    """check_and_reload_wfo_output MUSS _fetch_gist_file_content nutzen
    (nicht roh f.get('content'))."""
    from pathlib import Path
    src = Path(__file__).parent.parent / "app" / "persistence.py"
    body = src.read_text(encoding="utf-8")
    fn_start = body.index("def check_and_reload_wfo_output")
    next_def = body.find("\ndef ", fn_start + 50)
    fn_end = next_def if next_def != -1 else len(body)
    fn_body = body[fn_start:fn_end]
    assert "_fetch_gist_file_content" in fn_body, (
        "R-A51: WFO-Reload muss _fetch_gist_file_content (raw_url-Fallback) nutzen"
    )
    assert "reloaded_count" in fn_body, (
        "R-A51: conditional Marker-Update via reloaded_count"
    )


def test_r_a51_old_raw_content_pattern_gone():
    """Regression: alter 'if f and f.get(\"content\")' Pattern im Reload-Loop weg."""
    from pathlib import Path
    src = Path(__file__).parent.parent / "app" / "persistence.py"
    body = src.read_text(encoding="utf-8")
    fn_start = body.index("def check_and_reload_wfo_output")
    next_def = body.find("\ndef ", fn_start + 50)
    fn_end = next_def if next_def != -1 else len(body)
    fn_body = body[fn_start:fn_end]
    assert 'if f and f.get("content"):' not in fn_body, (
        "R-A51 REGRESSION: alter roher content-Check ist zurueck"
    )


# ---------------------------------------------------------------------------
# Helper-Verhalten: _fetch_gist_file_content mit Truncation
# ---------------------------------------------------------------------------

def test_r_a51_fetch_helper_uses_raw_url_when_truncated():
    """_fetch_gist_file_content faellt auf raw_url zurueck bei truncated+leer."""
    from app.persistence import _fetch_gist_file_content

    file_entry = {
        "content": "",  # leer (wie bei truncated gist-file)
        "truncated": True,
        "raw_url": "https://gist.githubusercontent.com/raw/wfo_status.json",
    }
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.text = '{"last_run": "2026-05-29T13:33:24", "aggregate": {"mean_oos_sharpe": 6.404}}'

    with patch("app.persistence.requests") as mock_req:
        mock_req.get.return_value = fake_resp
        content = _fetch_gist_file_content(file_entry, "fake_token")

    assert content is not None
    data = json.loads(content)
    assert data["last_run"] == "2026-05-29T13:33:24"
    assert data["aggregate"]["mean_oos_sharpe"] == 6.404
    # raw_url wurde aufgerufen
    mock_req.get.assert_called_once()


def test_r_a51_fetch_helper_returns_content_when_not_truncated():
    """Wenn nicht truncated + content da: direkt zurueckgeben, kein raw_url-Call."""
    from app.persistence import _fetch_gist_file_content

    file_entry = {
        "content": '{"x": 1}',
        "truncated": False,
        "raw_url": "https://should-not-be-called",
    }
    with patch("app.persistence.requests") as mock_req:
        content = _fetch_gist_file_content(file_entry, "tok")
        mock_req.get.assert_not_called()
    assert content == '{"x": 1}'


# ---------------------------------------------------------------------------
# Marker-Logik: nur setzen wenn reloaded_count > 0
# ---------------------------------------------------------------------------

def test_r_a51_marker_not_set_when_zero_reloaded(tmp_path, monkeypatch):
    """Wenn alle Files leer/truncated-fail: Marker NICHT setzen, return False."""
    import app.persistence as P

    storage = {}
    monkeypatch.setattr(P, "save_json", lambda fn, data: storage.__setitem__(fn, data))
    monkeypatch.setattr(P, "load_json", lambda fn: storage.get(fn))
    monkeypatch.setattr(P, "_get_token", lambda: "tok")
    monkeypatch.setattr(P, "_find_backup_gist", lambda t: "gist123")

    # Gist-Response: _wfo_meta hat neuen push, aber wfo_status content leer +
    # raw_url-Fetch schlaegt fehl (None)
    gist_files = {
        "_wfo_meta.json": {"content": json.dumps({"last_wfo_push": "2026-05-29T13:33:24"})},
        "wfo_status.json": {"content": "", "truncated": True, "raw_url": "https://x/ws"},
        "wfo_history.json": {"content": "", "truncated": True, "raw_url": "https://x/wh"},
    }
    fake_gist_resp = MagicMock()
    fake_gist_resp.status_code = 200
    fake_gist_resp.json.return_value = {"files": gist_files}

    # raw_url-Fetch gibt 404 → _fetch_gist_file_content returnt None
    fake_raw_resp = MagicMock()
    fake_raw_resp.status_code = 404

    def fake_get(url, **kwargs):
        if "gists/gist123" in url:
            return fake_gist_resp
        return fake_raw_resp  # raw_url fails

    with patch.object(P, "requests") as mock_req:
        mock_req.get.side_effect = fake_get
        result = P.check_and_reload_wfo_output()

    assert result is False, "Bei 0 reloaded Files muss False zurueckkommen"
    # Marker darf NICHT gesetzt sein
    assert "last_applied_wfo_push.json" not in storage or \
challenge_marker_absent(storage), "Marker darf bei 0-reload NICHT gesetzt werden"


def challenge_marker_absent(storage):
    """Helper: Marker entweder nicht da oder nicht auf neuen push gesetzt."""
    marker = storage.get("last_applied_wfo_push.json", {})
    return marker.get("last_wfo_push") != "2026-05-29T13:33:24"


def test_r_a51_marker_set_when_reload_succeeds(monkeypatch):
    """Wenn raw_url-Fetch erfolgreich: Files geladen + Marker gesetzt + True."""
    import app.persistence as P

    storage = {}
    monkeypatch.setattr(P, "save_json", lambda fn, data: storage.__setitem__(fn, data))
    monkeypatch.setattr(P, "load_json", lambda fn: storage.get(fn))
    monkeypatch.setattr(P, "_get_token", lambda: "tok")
    monkeypatch.setattr(P, "_find_backup_gist", lambda t: "gist123")

    gist_files = {
        "_wfo_meta.json": {"content": json.dumps({"last_wfo_push": "2026-05-29T13:33:24"})},
        "wfo_status.json": {"content": "", "truncated": True, "raw_url": "https://x/ws"},
        "wfo_history.json": {"content": "", "truncated": True, "raw_url": "https://x/wh"},
    }
    fake_gist_resp = MagicMock()
    fake_gist_resp.status_code = 200
    fake_gist_resp.json.return_value = {"files": gist_files}

    def fake_get(url, **kwargs):
        if "gists/gist123" in url:
            return fake_gist_resp
        # raw_url success
        r = MagicMock()
        r.status_code = 200
        if "ws" in url:
            r.text = json.dumps({"last_run": "2026-05-29T13:33:24",
                                 "aggregate": {"mean_oos_sharpe": 6.404}})
        else:
            r.text = json.dumps({"runs": []})
        return r

    # check_wfo_alerts mocken (kein Telegram im Test)
    monkeypatch.setattr("app.alerts.check_wfo_alerts", lambda: None, raising=False)

    with patch.object(P, "requests") as mock_req:
        mock_req.get.side_effect = fake_get
        result = P.check_and_reload_wfo_output()

    assert result is True
    # wfo_status.json wurde geladen mit frischen Daten
    assert storage["wfo_status.json"]["aggregate"]["mean_oos_sharpe"] == 6.404
    # Marker gesetzt
    assert storage["last_applied_wfo_push.json"]["last_wfo_push"] == "2026-05-29T13:33:24"
