from dataclasses import dataclass

from submission.client import ExtensionSubmissionError, candidate_exists_in_db


class PollingRequestError(Exception):
    """Transport-level failure only — mirrors ExtensionSubmissionError."""


@dataclass(frozen=True)
class PollResult:
    resolved: bool
    status: str | None = None  # "success" | "error", only meaningful when resolved
    status_message: str = ""


def poll_candidate_status(candidate) -> PollResult:
    """
    Decides whether one SUBMITTED candidate has landed, by asking the
    White Force check-candidate-exists API whether its phone is in the
    DB yet (submission.client.candidate_exists_in_db).

    exists=True -> resolved, success. exists=False -> not resolved yet
    (the ATS may still be parsing it); the caller just tries again on
    the next tick. Network/5xx problems raise PollingRequestError so an
    outage is never mistaken for "not found".
    """
    if not candidate.phone:
        return PollResult(resolved=False)

    try:
        exists = candidate_exists_in_db(candidate.phone, candidate.email)
    except ExtensionSubmissionError as exc:
        raise PollingRequestError(str(exc)) from exc

    if exists:
        return PollResult(resolved=True, status="success")
    return PollResult(resolved=False)
