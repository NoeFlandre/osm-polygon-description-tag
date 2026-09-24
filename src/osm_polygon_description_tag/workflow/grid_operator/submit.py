"""Submission-focused compatibility boundary for Grid job operations."""

from .submission import SubmissionPlan, plan_submission, submit_job

__all__ = ["SubmissionPlan", "plan_submission", "submit_job"]
