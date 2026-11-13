"""Failure stages shared by browser workers and the account scheduler."""


class CreditError(Exception):
    """Account has too little quota for this request: switch accounts, do not retry on it."""


class WorkQuotaError(Exception):
    """Dola 工作模式的免费额度用完了（每号 7 天 1 条，按首次使用起算）。

    和每日聊天额度无关：只锁这个号的工作模式，until 是 Dola 给的恢复时间（取不到时为 None）。
    """
    def __init__(self, message, until=None):
        self.until = until
        super().__init__(message)


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
    """Explicit assistant refusal or missing input; do not resubmit with another account.

    generation_started: the conversation had a creation card / video-gen flag when this
    was raised (None = unknown). Only a refusal with nothing started may be resubmitted.
    allow_account_retry=False: an explicit stop decision must also stop fallback retries.
    """
    def __init__(self, category, reply, *, generation_started=None, allow_account_retry=True):
        self.category = category
        self.reply = reply
        self.generation_started = generation_started
        self.allow_account_retry = allow_account_retry
        super().__init__(f"【{category}】{reply}")


class AIAccountUnavailableError(UpstreamReplyError):
    """AI 从未接单的最新回复判断账号暂不可用；有界换号，不推算或扣减额度。"""
    def __init__(self, category, reply):
        super().__init__(category, reply, generation_started=False)


class VideoSkillUnavailableError(UpstreamReplyError):
    """A finished plain-chat reply explicitly confirms video creation never started."""
    def __init__(self, reply):
        super().__init__("视频技能未生效", reply, generation_started=False)


class WorkVideoMismatchError(UpstreamReplyError):
    """工作模式已经产出视频，但实际时长或条数不符；保留成片，停止补做与换号。"""
    def __init__(self, reply, result):
        self.result = result
        super().__init__("成片参数不符", reply, generation_started=True, allow_account_retry=False)


def portrait_refusal(error) -> bool:
    """dola 以「肖像保护」拒绝、且确认还没开始生成：可以换号重提（不会重复出片）。"""
    if not isinstance(error, UpstreamReplyError):
        return False
    if not error.allow_account_retry:
        return False
    import failure_text
    if failure_text.label_for(getattr(error, "reply", "") or str(error)) != "肖像保护":
        return False
    if isinstance(error, UpstreamGenerationFailed):
        return True                       # 会话的最终卡片已判失败，原会话不会再出片
    return error.generation_started is False


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
