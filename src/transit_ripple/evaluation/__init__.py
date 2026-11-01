"""Evaluation framework (M6): detection vs. MTA alerts, surface impact placebo test,
camera accuracy, recommendation replay, explanation consistency, pipeline reliability."""

from .report import build_report, explanation_violations, to_markdown, write_report

__all__ = ["build_report", "explanation_violations", "to_markdown", "write_report"]
