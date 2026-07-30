"""Step-log tests.

Added after a real escape: PII redaction was applied to the *serialised JSON*,
which turned `"ts": 1785360762.05` into `178******60.05` and mangled UUID call
ids. Every one of 166 log records was unparseable — the structured trace, which
is the thing you walk through in a demo, was silently worthless.

So the contract is: **every line must be valid JSON, and PII must still be
masked.** Both directions are asserted here.
"""

from __future__ import annotations

import json

import pytest

from app.steplog import redact, step


class TestRedaction:
    @pytest.mark.parametrize("raw,must_not_contain", [
        ("call +919800000001 now", "9800000001"),
        ("borrower on 09812345678", "9812345678"),
        ("phone 9876543210", "9876543210"),
    ])
    def test_masks_indian_mobile_numbers(self, raw, must_not_contain):
        out = redact(raw)
        assert must_not_contain not in out
        assert "*" in out

    def test_masks_loan_ids(self):
        out = redact("account PL0098 is overdue")
        assert "PL0098" not in out
        assert out.startswith("account PL")

    @pytest.mark.parametrize("safe", [
        "1785360762.0594416",                        # epoch timestamp
        "e711a86b-4571-b02d-96d441d0ef9c",           # uuid fragment
        "2026-08-08",                                # iso date
        "latency 1142.3 ms",                         # measurement
        "amount 4500",                               # 4 digits, not a phone
    ])
    def test_leaves_structural_values_alone(self, safe):
        """The bug: a greedy phone pattern ate timestamps and UUIDs."""
        assert redact(safe) == safe


class TestStepRecords:
    def test_every_record_is_valid_json(self, tmp_path, monkeypatch):
        """The contract that was broken. Non-negotiable."""
        import app.steplog as sl

        log = tmp_path / "steps.jsonl"
        monkeypatch.setattr(sl, "_path", log)

        sl.step("call.started", "e711a86b-4571-b02d-96d441d0ef9c",
                loan_id="PL0098", phone="+919800000001")
        sl.step("stt.transcript", "e711a86b-4571-b02d-96d441d0ef9c",
                text="mera number 9876543210 hai", confidence=0.97)
        sl.step("tool.executed", None, tool="send_payment_link", ok=True,
                nested={"phone": "+919800000001", "amount": 4500})

        lines = [ln for ln in log.read_text(encoding="utf-8").splitlines() if ln.strip()]
        assert len(lines) == 3
        for line in lines:
            json.loads(line)   # must not raise

    def test_timestamps_and_call_ids_survive_verbatim(self, tmp_path, monkeypatch):
        import app.steplog as sl

        log = tmp_path / "steps.jsonl"
        monkeypatch.setattr(sl, "_path", log)
        call_id = "e711a86b-4571-b02d-96d441d0ef9c"
        sl.step("llm.first_token", call_id, ms=231.4)

        rec = json.loads(log.read_text(encoding="utf-8").strip())
        assert rec["call_id"] == call_id, "call_id is the correlation key; it must not be masked"
        assert isinstance(rec["ts"], float)
        assert rec["ms"] == 231.4

    def test_pii_is_still_masked_inside_records(self, tmp_path, monkeypatch):
        import app.steplog as sl

        log = tmp_path / "steps.jsonl"
        monkeypatch.setattr(sl, "_path", log)
        sl.step("dialer.precheck", "c1", phone="+919800000001", loan_id="PL0098",
                nested={"contact": "9876543210"})

        raw = log.read_text(encoding="utf-8")
        rec = json.loads(raw.strip())
        assert "9800000001" not in raw
        assert "9876543210" not in raw
        assert "PL0098" not in raw
        # ...and the structure is intact, not flattened into a mangled string.
        assert isinstance(rec["nested"], dict)

    def test_returns_the_record_for_fan_out(self, tmp_path, monkeypatch):
        import app.steplog as sl

        monkeypatch.setattr(sl, "_path", tmp_path / "steps.jsonl")
        rec = sl.step("tts.first_audio", "c1", ms=236.3)
        assert rec["stage"] == "tts.first_audio"
        assert rec["ms"] == 236.3
