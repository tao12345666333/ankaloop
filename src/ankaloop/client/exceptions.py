"""AnkaLoop Client Exceptions.

Custom exceptions for AnkaLoop client operations.
"""

from __future__ import annotations


class AnkaloopClientError(Exception):
    """Base exception for AnkaLoop client errors."""

    def __init__(self, message: str, code: str | None = None, details: dict | None = None):
        """Initialize the exception.

        Args:
            message: Error message.
            code: Optional error code.
            details: Optional additional details.
        """
        super().__init__(message)
        self.message = message
        self.code = code or "CLIENT_ERROR"
        self.details = details or {}


class ConnectionError(AnkaloopClientError):
    """Raised when connection to server fails."""

    def __init__(self, message: str = "Failed to connect to server", **kwargs):
        """Initialize the exception.

        Args:
            message: Error message.
            **kwargs: Additional arguments for AnkaloopClientError.
        """
        super().__init__(message, code="CONNECTION_ERROR", **kwargs)


class TimeoutError(AnkaloopClientError):
    """Raised when a request times out."""

    def __init__(self, message: str = "Request timed out", timeout: float | None = None, **kwargs):
        """Initialize the exception.

        Args:
            message: Error message.
            timeout: The timeout value that was exceeded.
            **kwargs: Additional arguments for AnkaloopClientError.
        """
        details = kwargs.pop("details", {})
        if timeout is not None:
            details["timeout"] = timeout
        super().__init__(message, code="TIMEOUT_ERROR", details=details, **kwargs)
        self.timeout = timeout


class SessionError(AnkaloopClientError):
    """Base exception for session-related errors."""

    def __init__(self, message: str, session_id: str | None = None, **kwargs):
        """Initialize the exception.

        Args:
            message: Error message.
            session_id: The session ID related to the error.
            **kwargs: Additional arguments for AnkaloopClientError.
        """
        details = kwargs.pop("details", {})
        if session_id:
            details["session_id"] = session_id
        super().__init__(message, details=details, **kwargs)
        self.session_id = session_id


class SessionNotFoundError(SessionError):
    """Raised when a session is not found."""

    def __init__(self, session_id: str):
        """Initialize the exception.

        Args:
            session_id: The session ID that was not found.
        """
        super().__init__(
            message=f"Session not found: {session_id}",
            session_id=session_id,
            code="SESSION_NOT_FOUND",
        )


class SessionBusyError(SessionError):
    """Raised when a session is busy and cannot accept new requests."""

    def __init__(self, session_id: str):
        """Initialize the exception.

        Args:
            session_id: The session ID that is busy.
        """
        super().__init__(
            message=f"Session is busy: {session_id}",
            session_id=session_id,
            code="SESSION_BUSY",
        )


class MaxSessionsError(AnkaloopClientError):
    """Raised when maximum session limit is reached."""

    def __init__(self, max_sessions: int):
        """Initialize the exception.

        Args:
            max_sessions: The maximum number of sessions allowed.
        """
        super().__init__(
            message=f"Maximum sessions limit reached: {max_sessions}",
            code="MAX_SESSIONS_REACHED",
            details={"max_sessions": max_sessions},
        )
        self.max_sessions = max_sessions


class ServerError(AnkaloopClientError):
    """Raised when the server returns an error."""

    def __init__(
        self,
        message: str = "Server error",
        status_code: int | None = None,
        **kwargs,
    ):
        """Initialize the exception.

        Args:
            message: Error message.
            status_code: HTTP status code.
            **kwargs: Additional arguments for AnkaloopClientError.
        """
        details = kwargs.pop("details", {})
        if status_code is not None:
            details["status_code"] = status_code
        super().__init__(message, code="SERVER_ERROR", details=details, **kwargs)
        self.status_code = status_code


class AuthenticationError(AnkaloopClientError):
    """Raised when authentication fails."""

    def __init__(self, message: str = "Authentication failed", **kwargs):
        """Initialize the exception.

        Args:
            message: Error message.
            **kwargs: Additional arguments for AnkaloopClientError.
        """
        super().__init__(message, code="AUTHENTICATION_ERROR", **kwargs)


class PromptError(AnkaloopClientError):
    """Raised when a prompt request fails."""

    def __init__(
        self,
        message: str = "Prompt request failed",
        session_id: str | None = None,
        **kwargs,
    ):
        """Initialize the exception.

        Args:
            message: Error message.
            session_id: The session ID related to the error.
            **kwargs: Additional arguments for AnkaloopClientError.
        """
        details = kwargs.pop("details", {})
        if session_id:
            details["session_id"] = session_id
        code = kwargs.pop("code", "PROMPT_ERROR")
        super().__init__(message, code=code, details=details, **kwargs)
        self.session_id = session_id


class StreamTimeoutError(PromptError):
    """Raised when a prompt times out waiting for response data.

    The turn itself may still be running server-side; only the client's
    wait for the response was interrupted.
    """

    def __init__(
        self,
        message: str = "Stream timed out",
        session_id: str | None = None,
        timeout: float | None = None,
        **kwargs,
    ):
        """Initialize the exception.

        Args:
            message: Error message.
            session_id: The session ID related to the error.
            timeout: The idle timeout in seconds that was exceeded, if known.
            **kwargs: Additional arguments for PromptError.
        """
        details = kwargs.pop("details", {})
        if timeout is not None:
            details["timeout"] = timeout
        super().__init__(message, session_id=session_id, code="STREAM_TIMEOUT", details=details, **kwargs)
        self.timeout = timeout
