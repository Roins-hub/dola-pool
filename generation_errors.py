"""Failure stages shared by browser workers and the account scheduler."""


class PreSubmissionUploadError(TimeoutError):
    """Reference upload failed before a video generation request was submitted.

    The scheduler may try another account; generic TimeoutError remains ambiguous
    and must not cause resubmission.
    """


class PreSubmissionParametersError(TimeoutError):
    """Requested video settings could not be confirmed before sending the prompt."""
