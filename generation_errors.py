"""Failure stages shared by browser workers and the account scheduler."""


class PreSubmissionUploadError(TimeoutError):
    """Reference upload failed before a video generation request was submitted.

    The scheduler may try another account; generic TimeoutError remains ambiguous
    and must not cause resubmission.
    """


class PreSubmissionParametersError(TimeoutError):
    """Requested video settings could not be confirmed before sending the prompt."""


class UpstreamReplyError(RuntimeError):
    """Explicit assistant refusal or missing input; do not resubmit with another account."""
    def __init__(self, category, reply):
        self.category = category
        self.reply = reply
        super().__init__(f"【{category}】{reply}")


class UploadRouteError(PreSubmissionUploadError):
    """Transport failure to an upload host, safe to pause the shared route."""
    def __init__(self, host, code, stage="ApplyImageUpload"):
        self.host, self.code = host, code
        super().__init__(f"参考图片上传连接失败（尚未提交生成）：{stage} {host} {code}")
