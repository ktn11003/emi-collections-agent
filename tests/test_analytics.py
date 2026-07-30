"""Post-call analytics pipeline tests.

Added after a real escape: ``step()`` takes ``stage`` as its first positional
parameter, and the pipeline was also passing ``stage=`` as a keyword. That
``TypeError`` made analytics fail for **every** call, and nothing caught it
because this module had no tests. These run in offline mode, so they exercise the
full pipeline shape without touching the network.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from app.analytics.pipeline import analyse_all_pending, analyse_call, portfolio_report
from app.db.base import session_scope
from app.db.models import AnalyticsReport, Channel, Disposition, Speaker
from app.db.repo import add_turn, create_call, finalise_call, get_call

pytestmark = pytest.mark.asyncio


def _completed_call(borrower, *, disposition: Disposition = Disposition.PTP) -> str:
    """A finished call with a realistic code-mixed transcript."""
    with session_scope() as s:
        call = create_call(
            s,
            correlation_id=f"analytics-{uuid.uuid4()}",
            loan_id=borrower.loan_id,
            channel=Channel.BROWSER,
            script_language="hi-IN",
            compliance={"in_window": True, "consent_on_file": True},
        )
        add_turn(s, call, speaker=Speaker.BOT, language="hi-IN", text=(
            "Namaste Rahul ji, main Priya bol rahi hoon Piramal Finance se. "
            "Yeh call quality ke liye record ki ja rahi hai. "
            "Aapki 4,500 rupees ki EMI 5 tareekh ko due thi."
        ))
        add_turn(s, call, speaker=Speaker.BORROWER, language="hi-IN",
                 language_confidence=0.97,
                 text="Abhi paise nahi hain, salary late ho gayi hai is mahine.")
        add_turn(s, call, speaker=Speaker.BOT, language="hi-IN",
                 text="Samajh sakti hoon. Kis tareekh tak kar payenge?")
        add_turn(s, call, speaker=Speaker.BORROWER, language="hi-IN",
                 language_confidence=0.95,
                 text="Aath tareekh ko kar dunga.")
        finalise_call(s, call, disposition=disposition,
                      latency_stats={"p50_end_to_first_audio_ms": 900.0})
        return call.id


class TestAnalyseCall:
    async def test_pipeline_runs_and_persists(self, borrower):
        """The regression guard: this raised TypeError for every call."""
        call_id = _completed_call(borrower)
        result = await analyse_call(call_id)

        assert result.call_id == call_id
        assert result.summary, "a summary must be produced"
        assert result.sentiment in ("positive", "neutral", "negative")
        assert 0.0 <= result.qa_score <= 100.0
        assert result.diarized_turns, "diarized turns must be carried through"
        # Provenance: which model versions produced this report.
        assert result.model_versions.get("llm")

        with session_scope() as s:
            stored = s.scalar(select(AnalyticsReport).where(AnalyticsReport.call_id == call_id))
            assert stored is not None
            assert stored.summary == result.summary
            assert stored.qa_score == result.qa_score

    async def test_writes_summary_back_onto_the_call(self, borrower):
        call_id = _completed_call(borrower)
        await analyse_call(call_id)
        with session_scope() as s:
            call = get_call(s, call_id)
            assert call.sentiment, "sentiment should land on the CDR for dashboards"
            assert call.summary

    async def test_scores_compliance_from_the_transcript(self, borrower):
        """QA is deterministic — a rule, not the model's opinion."""
        call_id = _completed_call(borrower)
        result = await analyse_call(call_id)
        flags = result.compliance_flags
        assert flags["disclosed_recording"] is True    # the opening line says so
        assert flags["identified_self"] is True        # "Piramal" / "Priya"
        assert flags["no_threat"] is True
        assert result.qa_score >= 87.5

    async def test_rerun_updates_rather_than_duplicates(self, borrower):
        call_id = _completed_call(borrower)
        await analyse_call(call_id)
        await analyse_call(call_id)
        with session_scope() as s:
            rows = s.scalars(
                select(AnalyticsReport).where(AnalyticsReport.call_id == call_id)
            ).all()
        assert len(rows) == 1, "analytics_reports.call_id is unique per call"

    async def test_unknown_call_raises(self):
        with pytest.raises(ValueError, match="unknown call"):
            await analyse_call("does-not-exist")

    async def test_call_with_no_turns_is_skipped_not_crashed(self, borrower):
        with session_scope() as s:
            call = create_call(
                s, correlation_id=f"empty-{uuid.uuid4()}", loan_id=borrower.loan_id
            )
            finalise_call(s, call, disposition=Disposition.NO_ANSWER)
            call_id = call.id
        result = await analyse_call(call_id)
        assert result.summary == ""
        assert result.diarized_turns == []


class TestBatch:
    async def test_scores_only_unscored_calls(self, borrower):
        """The overnight job: every call scored, none scored twice."""
        first = _completed_call(borrower)
        await analyse_call(first)
        second = _completed_call(borrower, disposition=Disposition.LINK_SENT)

        results = await analyse_all_pending(limit=10)
        scored_ids = {r.call_id for r in results}

        assert second in scored_ids
        assert first not in scored_ids, "an already-scored call must not be re-run"

    async def test_portfolio_report_is_computable(self, borrower):
        await analyse_call(_completed_call(borrower))
        report = portfolio_report()

        for key in ("total_calls", "calls_scored", "avg_qa_score",
                    "qa_coverage_pct", "sentiment_mix", "ptp_rate_pct"):
            assert key in report, key
        assert report["calls_scored"] >= 1
        assert 0.0 <= report["qa_coverage_pct"] <= 100.0
