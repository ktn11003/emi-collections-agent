"""Tests for recovering tool calls the model emitted as speech.

Observed live: sarvam-105b intermittently writes a function call into
``content`` instead of ``tool_calls``. Unhandled, the bot reads JSON to the
borrower and the promise-to-pay is lost. Both failures are asserted against here.
"""

from __future__ import annotations

import pytest

from app.agent.salvage import looks_like_tool_call, salvage


class TestDetection:
    @pytest.mark.parametrize("leak", [
        '[{"name": "schedule_ptp", "arguments": {"loan_id": "PL0098", "promised_date": "2026-08-08"}}]',
        '{"loan_id": "PL0098", "amount": 4500, "channel": "WHATSAPP"}',
        '[ { "loan_id":',                       # split mid-stream by the aggregator
        '"PL0098", "promised_date":',
        '}]',
        'schedule_ptp(loan_id="PL0098")',
        '```json\n{"disposition": "PTP"}\n```',
    ])
    def test_detects_tool_call_text(self, leak):
        assert looks_like_tool_call(leak), leak

    @pytest.mark.parametrize("speech", [
        "Namaste Rahul ji, main Priya bol rahi hoon Piramal Finance se.",
        "Aapki 4,500 rupees ki EMI 5 tareekh ko due thi.",
        "Theek hai, main 8 tareekh note kar rahi hoon.",
        "क्या आप आज payment कर सकते हैं?",
        "Main aapko payment link WhatsApp par bhej deti hoon.",
        "Dhanyavaad. Aapka din shubh ho.",
    ])
    def test_does_not_flag_real_speech(self, speech):
        """A false positive here silences the bot, so this must stay tight."""
        assert not looks_like_tool_call(speech), speech


class TestSalvage:
    def test_recovers_openai_shape(self):
        calls = salvage(
            '[{"name": "schedule_ptp", "arguments": '
            '{"loan_id": "PL0098", "promised_date": "2026-08-08", "amount": 4500}}]'
        )
        assert len(calls) == 1
        assert calls[0].name == "schedule_ptp"
        assert calls[0].arguments["loan_id"] == "PL0098"
        assert calls[0].arguments["promised_date"] == "2026-08-08"

    def test_infers_tool_from_bare_arguments(self):
        calls = salvage('{"loan_id": "PL0098", "promised_date": "2026-08-08", "amount": 4500}')
        assert [c.name for c in calls] == ["schedule_ptp"]

    def test_infers_payment_link(self):
        calls = salvage('{"loan_id": "PL0102", "amount": 7800, "channel": "WHATSAPP"}')
        assert [c.name for c in calls] == ["send_payment_link"]

    def test_normalises_mangled_keys(self):
        """The model sometimes drops underscores."""
        calls = salvage('{"name": "schedule_ptp", "arguments": {"loanid": "PL0098", "promiseddate": "2026-08-08"}}')
        assert calls[0].arguments["loan_id"] == "PL0098"
        assert calls[0].arguments["promised_date"] == "2026-08-08"

    def test_fills_loan_id_from_call_context(self):
        """The call already knows whose account it is."""
        calls = salvage('{"promised_date": "2026-08-08", "amount": 4500}', default_loan_id="PL0098")
        assert calls[0].arguments["loan_id"] == "PL0098"

    def test_recovers_multiple_calls(self):
        calls = salvage(
            '[{"name":"schedule_ptp","arguments":{"loan_id":"PL1","promised_date":"2026-08-08"}},'
            ' {"name":"send_payment_link","arguments":{"loan_id":"PL1","amount":4500}}]'
        )
        assert {c.name for c in calls} == {"schedule_ptp", "send_payment_link"}

    def test_ignores_unknown_tools(self):
        assert salvage('{"name": "wire_transfer", "arguments": {"loan_id": "PL1", "iban": "X"}}') == []

    def test_ignores_undispatchable_fragments(self):
        assert salvage("Namaste Rahul ji") == []
        assert salvage('{"foo": "bar"}') == []
        assert salvage("") == []

    def test_survives_truncated_json(self):
        """A stream cut mid-object must not raise."""
        assert salvage('{"name": "schedule_ptp", "arguments": {"loan_id": "PL1"') == []
