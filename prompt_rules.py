"""Versioned reference data, never executable instructions downloaded at runtime."""
import config

RULES_VERSION = '2026-10-03.1'
_RULES = (
    {'id':'visual-action','scope':'prompt-quality','evidence':'community-guide',
     'source':'https://github.com/mqrox/seedance-2.0-prompt-skill',
     'checked_at':'2026-10-03',
     'text':'Mô tả chủ thể, hành động nhìn thấy được, bối cảnh và chuyển động máy quay khi phù hợp ý tưởng. Thiếu góc máy là gợi ý chất lượng, không phải lý do chặn.'},
    {'id':'reference-role','scope':'prompt-quality','evidence':'community-guide',
     'source':'https://github.com/lukasersil/seedance-25', 'checked_at':'2026-10-03',
     'text':'Ghi rõ vai trò và thứ tự ảnh tham chiếu. Nếu chỉ có metadata, không khẳng định đã kiểm tra nội dung pixel hoặc tính đồng nhất nhân vật.'},
    {'id':'scene-pacing','scope':'prompt-quality','evidence':'community-guide',
     'source':'https://github.com/Emily2040/seedance-2.0', 'checked_at':'2026-10-03',
     'text':'Đánh giá số hành động và chuyển cảnh so với thời lượng mục tiêu; gợi ý giảm mật độ khi cần, không tự thêm tình tiết hoặc ép one-take.'},
    {'id':'first-message-duration','scope':'dola-first-message','evidence':'local-heuristic',
     'source':'extensions/dola30/必读.txt', 'checked_at':'2026-10-03',
     'text':'Extension local khuyên hạn chế mốc thời gian trong lượt gửi đầu của conversation mới. Đây là kinh nghiệm chưa bảo đảm; không xóa timeline/storyboard của người dùng một cách tuyệt đối.'},
)


def rules_for_context(context: dict) -> list[dict]:
    rules = [dict(r) for r in _RULES if r['scope'] != 'dola-first-message' or context.get('new_conversation') is True]
    rules.append({'id':'backend-capabilities','scope':'dola-studio-backend',
                  'evidence':'current-code','source':'config.py', 'checked_at':'2026-10-03',
                  'text':f'Studio chấp nhận các thời lượng {list(config.ALL_DURATIONS)} giây; model được backend xác thực. Đây là lựa chọn backend, không bảo đảm Dola trả đủ thời lượng hoặc hỗ trợ mọi tổ hợp. Không lấy giới hạn API BytePlus/Volcengine làm giới hạn Dola.'})
    return rules
