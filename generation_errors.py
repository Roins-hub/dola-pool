"""Failure stages shared by browser workers and the account scheduler."""


class PreSubmissionUploadError(TimeoutError):
    """Reference upload failed before a video generation request was submitted.

    The scheduler may try another account; generic TimeoutError remains ambiguous
    and must not cause resubmission.
    """


class PreSubmissionParametersError(TimeoutError):
    """Requested video settings could not be confirmed before sending the prompt."""


class RegionRestrictedError(Exception):
    """Dola redirected to /security/region-restricted: the proxy exit's region, not a logout.

    2026-10-29: the "日本动态" proxy sessions exited in Chile/Brazil/South Africa.
    Raised before anything is submitted.
    """


class UpstreamReplyError(RuntimeError):
    """Explicit assistant refusal or missing input; do not resubmit with another account."""
    def __init__(self, category, reply):
        self.category = category
        self.reply = reply
        super().__init__(f"【{category}】{reply}")


class UpstreamGenerationFailed(UpstreamReplyError):
    """The conversation's final creation card reports a failed task without a video.

    Only this confirmed state may be retried on another account; the original
    conversation is final, so a new submission cannot duplicate a pending video.
    """
    def __init__(self, category, reply, *, code, conversation_id, retryable, not_charged):
        self.code, self.conversation_id = code, conversation_id
        self.retryable, self.not_charged = retryable, not_charged
        charge = "上游声明未扣积分" if not_charged else "上游未说明扣费"
        super().__init__(category, f"{reply}（fail_code={code}，{charge}；会话 {conversation_id} 已确认结束）")


class UploadRouteError(PreSubmissionUploadError):
    """Transport failure to an upload host, safe to pause the shared route."""
    def __init__(self, host, code, stage="ApplyImageUpload", detail=""):
        self.host, self.code, self.stage, self.detail = host, code, stage, detail
        suffix = f"（{detail}）" if detail else ""
        super().__init__(f"参考图片上传连接失败（尚未提交生成）：{stage} {host} {code}{suffix}")
