class SessionAlreadyRunningError(Exception):
    """
    Raised when an attempt is made to start a new CrawlSession while
    another session is still in the 'running' status.

    This is the enforcement point for the "only one active session at a
    time" rule — a manual test session left running during the day must
    not silently collide with the 10pm scheduled session.
    """
    pass


class SessionExpiredError(Exception):
    """
    Raised by the crawler's browser layer when Shine's site indicates
    the saved login session (storageState cookies) is no longer valid —
    e.g. a redirect to the login page, or a specific "please log in"
    DOM marker.

    This is intentionally a distinct exception type (not a generic
    request failure) so the crawl task can catch it specifically and
    abort the night's crawl with an alert, rather than retrying against
    a login wall and burning CPU for hours.
    """
    pass