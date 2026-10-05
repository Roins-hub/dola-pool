# Tạo ảnh tham chiếu bằng 9router

Đã cấu hình và triển khai trên Dola Studio tại http://127.0.0.1:20129.

- Gateway: http://127.0.0.1:20128/v1.
- Model ảnh: antigravity/gemini-3.1-flash-image.
- API key dùng lại key gateway hiện có; lưu mã hóa trong AI settings, không cần đưa key vào chat.
- Tạo video → Ảnh tham chiếu → Tạo ảnh tham chiếu bằng AI.
- Mỗi dòng là một prompt, tối đa 8 dòng/lượt; tối đa 4 ảnh xử lý đồng thời trong mỗi lượt.
- Kết quả giữ thứ tự prompt; ảnh lỗi có thông báo riêng, không làm mất ảnh thành công.
- Backend xác minh PNG/JPEG/WEBP bằng Pillow, giới hạn 15 MB/ảnh; phản hồi JSON/base64 không hợp lệ trả lỗi rõ ràng.
- Endpoint yêu cầu phiên đăng nhập: POST /api/admin/images/generate, body {"prompts":["..."],"size":"1024x1024"}.
- Không thay đổi nhà cung cấp AI văn bản đang hoạt động.

## Kiểm chứng ngày 05/10/2026

455 test pass, database/profile/config được cô lập trước import app.
Live: 3 ảnh tham chiếu tạo qua script gateway; 2 ảnh tạo song song từ giao diện Studio, tự thêm vào bản nháp.
Database/cấu hình đã sao lưu trước restart, không có job đang chạy lúc restart.

## Luồng demo video 30 giây

Giữ duration=30 và model=seedance-2.5 trong cấu hình API. Mô tả cảnh trong prompt; không cần lặp số giây trong câu chữ. Lượt live mới được Dola nhận, thông báo tiêu hao 2 điểm, dự kiến 25 phút. Chưa khẳng định thời lượng thành phẩm trước khi đo bằng ffprobe.

Ảnh mặt người trong lượt thử bị Dola từ chối vì chưa xác thực. Demo sử dụng ảnh bối cảnh và mô tả các nhân vật hư cấu bằng văn bản; không tìm cách vượt kiểm tra quyền sử dụng hình ảnh.

Không tự gửi lại khi mất phản hồi. Các cảnh demo có request_id cố định; kiểm tra job/conversation hiện có trước mọi thao tác gửi.

Bộ phân loại đã nhận câu hỏi thương lượng thời lượng bằng tiếng Việt. Câu hỏi riêng không còn hiển thị là đang dựng video; nếu cùng conversation có thông báo Dola nhận tạo thật, thông báo nhận tạo được ưu tiên. Cập nhật đã phục hồi 3 conversation trong process mới với ID, tài khoản và deadline giữ nguyên.
