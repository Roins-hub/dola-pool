# Dola Studio cá nhân

App mở vào Tạo video. Nhập mô tả, chọn model/thời lượng/khung hình và ảnh tham chiếu nếu cần. Nút **Tạo video** gửi tạo trực tiếp, không gọi AI kiểm tra. **Kiểm tra nội dung** là thao tác riêng, tùy chọn. Backend vẫn kiểm tra đăng nhập, dữ liệu, model/thời lượng và ảnh; Dola vẫn xử lý nội dung theo quy định của dịch vụ.

Nhập **Bạn muốn sửa gì?** để AI chỉnh bản đang chọn. Bản đề xuất không tự ghi đè prompt. Có sửa tiếp, so sánh, áp dụng, hoàn tác, bản gốc và lịch sử. Prompt/lịch sử được lưu trên trình duyệt; ảnh được lưu bằng IndexedDB. Trình duyệt xóa dữ liệu hoặc hết dung lượng có thể làm mất bản nháp. Studio báo khi lưu thất bại.

## Tạo hàng loạt và thông báo Dola

Chọn **Một prompt**, đặt **Số video mỗi prompt** để tạo nhiều kết quả riêng. Hoặc chọn **Danh sách prompt / TXT**: mỗi dòng là một prompt, bỏ dòng trống; giữ dòng trùng. File TXT cần UTF-8 và tối đa 1 MB. Mỗi prompt tạo 1–10 bản, tổng tối đa 50 video/mẻ. Model, thời lượng, tỉ lệ và ảnh tham chiếu áp dụng cho cả mẻ.

Bấm **Kiểm tra nội dung** nếu muốn phân tích từng prompt trước. Kết quả kiểm tra chỉ hỗ trợ duyệt; nút tạo mẻ không tự gọi hoặc chờ AI. Trước khi tạo, xác nhận số video và việc có thể dùng thêm điểm. Mẻ được lưu đầy đủ trong SQLite trước khi worker gửi; tải lại hoặc đóng trang không dừng hàng đợi. Mã mẻ giữ nguyên khi thử lại cùng bản nháp sau mất phản hồi. Thay đổi nội dung với cùng mã bị từ chối. Xóa video trong mẻ rồi thử lại mã cũ không tạo lại video đã xóa.

**Video của tôi** mở mẻ vừa tạo, có số job hoàn tất, đang chạy, chưa rõ kết quả và thất bại. Bấm **Xem tất cả video** để ra khỏi bộ lọc. Mỗi video có trạng thái, thanh tiến độ theo bước và phản hồi Dola riêng.

Thanh tiến độ không suy ra phần trăm từ thời gian. Dola chưa có trường phần trăm render được xác minh, nên tác vụ đang chạy dùng thanh chờ không xác định. Thông báo gốc, trạng thái creation và thời gian Dola dự kiến được hiển thị khi upstream có dữ liệu, kèm lịch sử tối đa 20 lần thay đổi. Lịch sử giữ qua restart; lỗi kết nối vẫn giữ thông tin đã nhận. URL và các chuỗi token/session phổ biến bị che khỏi nội dung hiển thị; không gửi raw chain ra UI.

Studio lấy trạng thái backend mỗi 3 giây khi theo dõi hoặc ở Video của tôi (nếu bật tự làm mới). Backend Pure API mặc định hỏi Dola mỗi 10 giây; `DOLA_PURE_POLL_INTERVAL` giữ quyền cấu hình hiện có. Đây là cập nhật gần thời gian thực qua polling, không phải push/SSE trực tiếp. Không diễn giải thời gian dự kiến là bảo đảm hoàn tất.

## Kết quả chưa rõ và video ngắn

- Mặc định theo dõi 1.800 giây từ lúc gửi yêu cầu (bao gồm thời gian chờ conversation). Không tự gửi lại khi polling lỗi, hết thời gian chờ hoặc mất xác nhận gửi.
- Trạng thái `unknown` có nghĩa kết quả chưa rõ, không phải thất bại upstream. **Kiểm tra lại kết quả** tiếp tục conversation/tài khoản đã lưu với một cửa sổ theo dõi mới.
- Nếu chưa có conversation, tool giữ thông tin gửi nhưng không tự tạo lại. Cần kiểm tra tài khoản Dola để xác định kết quả trước khi tạo yêu cầu mới.
- Video thiếu thời lượng vẫn được lưu và cho tải. **Tạo tiếp và ghép** cần xác nhận dùng thêm điểm; không bảo đảm cảnh liền mạch. Nhấn lại mở cùng đoạn tiếp, không tạo thêm một job khác. Chỉ ghép các đoạn Dola đã trả về; không tự tạo bù.
- Backend khôi phục task từ SQLite sau khi restart. Giữ nguyên tài khoản và conversation; không gửi prompt lại khi đã có conversation.

## Cài đặt và tương thích

Giữ tài khoản Dola, kết nối AI đã lưu, proxy và chẩn đoán. UI quản lý API Key khách hàng và nhiều quản trị viên đã bỏ; API và cơ chế đăng nhập vẫn giữ. Sau khi restart server cần đăng nhập lại vì phiên đăng nhập nằm trong RAM.

`DOLA_OBSERVATION_SECONDS` mặc định `1800`. `DOLA_TRANSPORT_MODE=relay` giữ tuyến Pure API hiện có và không dùng proxy tài khoản; proxy vẫn dùng cho browser. `proxy` dùng proxy được chọn cho Pure API, không cài relay hook. Đổi mode cần restart; tuyến hiện tại được hiển thị trong Cài đặt. Proxy browser hỏng không tự chuyển sang direct.

API bổ sung, tương thích request cũ:

- `POST /api/admin/prompts/rewrite`: trường `instructions` tùy chọn, tối đa 4.000 ký tự.
- `POST /api/admin/videos/generations`: `request_id` tùy chọn để trả cùng task khi gửi lại cùng bản nháp. Studio tự giữ mã này khi mất phản hồi.
- `POST /api/admin/tasks/{id}/check-result`: chỉ phục hồi kết quả cũ.
- `POST /api/admin/tasks/{id}/continue`: JSON `confirm_cost=true`; `confirm_review=true` nếu nội dung cần xác nhận. Dùng thêm điểm chỉ sau xác nhận.
- `POST /api/admin/videos/batches`: multipart `batch` là JSON gồm `request_id`, `prompts` (mỗi phần tử có `prompt`, `preflight_token`, `confirm_review`), `copies`, `model`, `duration`, `ratio`; `images` dùng chung cho mẻ. Trả `batch_id` và danh sách task; mã yêu cầu dùng lại chỉ trả mẻ cũ.
- `GET /api/admin/videos/batches/{batch_id}`: trạng thái đầy đủ của mẻ, kể cả sau restart.
- Task thêm `upstream_updates`, `last_poll_at`, `batch_id`, `batch_index`; bảng `video_batches` giữ biên nhận chống tạo trùng cả khi video đã bị xóa.
- Task thêm `unknown`, `phase`, thời lượng thực, thời lượng mục tiêu và trạng thái file. Migration SQLite chỉ thêm cột, giữ dữ liệu cũ.

Video preview/tải trong Studio dùng cùng origin đang mở, nên URL tunnel cũ lưu trong DB không làm hỏng link file nội bộ. Giao diện ghi kích thước/thời lượng thực, không coi upscale là native 1080p.

## Kiểm tra không dùng điểm thật

```sh
.venv/bin/python -m pytest tests -q
.venv/bin/python tests/run_studio_journey.py /tmp/dola-studio-evidence
```

Pytest cô lập DB, profile, cấu hình AI và credential store trước import. Journey dùng process mới, protocol Dola giả và AI HTTP transport giả; giữ scheduler, worker Pure API, SQLite, media download, FFmpeg và browser thật. Script sinh file H.264 15 giây, kiểm tra ghép ~30 giây, restart không gửi trùng, timeout/kiểm tra lại, draft/ảnh, rewrite/preflight, race response và desktop/mobile.

Hai script E2E cũ là entrypoint tương thích cho journey này. Script dịch/patch dùng một lần và HTML backup cũ đã được lưu ngoài repo trong thư mục triển khai ngày 02/10/2026. Không chạy các script cũ lên UI mới.

## Thiết kế Studio mới

Workspace có prompt/AI bên trái, ảnh tham chiếu bên phải và hàng đợi bảng/lưới cạnh preview. Vai trò ảnh là thông tin người dùng chọn; phân tích hiện đọc văn bản và metadata, không tuyên bố đã xem pixel. AI chỉ rõ đoạn gốc, lý do và thay thế; chọn từng gợi ý để dựng bản đề xuất, xem trước rồi áp dụng, có hoàn tác. Đoạn trích sai, trùng chưa rõ vị trí hoặc chồng lấn không tự thay.

Kiểm tra prompt mới gắn model, thời lượng, tỉ lệ và SHA-256 byte ảnh. Đổi đầu vào làm mất hiệu lực kết quả cũ. Request preflight JSON chỉ có prompt vẫn được nhận; với luồng gửi có yêu cầu preflight, token chỉ kiểm tra văn bản cần phân tích bổ sung cấu hình thực. Tạo trực tiếp từ Studio cá nhân không bắt buộc token AI; các endpoint công khai giữ gate hiện có. Biên nhận request đã có được trả trước gate hết hạn và không gọi lại AI. Cập nhật Dola hiển thị tiếng Việt; nguyên văn đã che secrets ở Chi tiết kỹ thuật.

## Thư mục prompt và xuất kết quả

Chọn **Thư mục TXT + ảnh**: mỗi nhóm chứa đúng một TXT UTF-8; toàn bộ nội dung nhiều dòng là một prompt. Mỗi nhóm dùng ảnh của chính thư mục đó, có thể chọn vai trò cho từng ảnh và sửa prompt trong bản xem trước. Duyệt trước khi tạo. File không hỗ trợ, nhiều TXT hoặc thiếu TXT được báo lỗi và giữ bản nháp trước. Tối đa 50 video mỗi mẻ và 1–10 bản mỗi prompt; giới hạn ảnh toàn mẻ dùng cấu hình backend hiện có. Bản nháp nhóm và ảnh được giữ trong trình duyệt. Mobile không có directory picker thì dùng danh sách TXT và chọn ảnh chung.

Ở **Video của tôi**, **Tải ZIP kết quả** tải các file đã hoàn tất của mẻ. `ket-qua.json` ghi các video đã đóng gói và file bị thiếu/chưa hoàn tất. ZIP được ghi ra file tạm trên server, xóa sau khi trả response; file symlink ra ngoài kho media bị bỏ qua. Trình duyệt nhận ZIP để tải, nên mẻ rất lớn có thể cần chia nhỏ để tránh dùng nhiều bộ nhớ trình duyệt.

**Cài đặt → Nâng cao: proxy → Thư mục xuất video** cho chọn thư mục đã tồn tại và có quyền ghi trên máy chạy Studio. Video hoàn tất sau đó được sao chép, giữ nguyên media gốc và dữ liệu tác vụ. Lỗi sao chép không làm mất kết quả; tải bản gốc vẫn hoạt động. **Mở thư mục** chỉ dùng với truy cập localhost trực tiếp đã đăng nhập; truy cập từ xa dùng ZIP.

Kho tài khoản được bật/tắt độc lập với trạng thái đăng nhập và hạn mức. Tắt kho chặn nhận video mới, không dừng conversation đã gửi. Nếu tất cả kho/tài khoản bị tắt, video mới giữ trạng thái Chờ tài khoản. Nhập Google/Facebook hàng loạt có kiểm tra từng dòng và loại trùng trước đăng nhập; tiến độ hiển thị từ job đăng nhập thật, không gửi Hello khi chỉ xem danh sách.

Nhật ký nâng cao lấy trạng thái và phản hồi đã lưu, lọc theo tác vụ/tài khoản/mức. Đây không phải toàn bộ stdout của process. Tuyến Relay được ghi riêng, không được diễn giải thành Proxy tài khoản đang hoạt động. Proxy đã gán bị hết node báo lỗi, không tự đổi sang tuyến mặc định.

## Nghiệm thu redesign 03/10/2026

Regression 415 kiểm tra đã qua; browser giữ scheduler, worker, SQLite và FFmpeg thật, gồm lỗi polling, kết quả sau 450 giây bằng clock fixture, process mới, gửi chưa rõ kết quả, folder có ảnh riêng và ZIP. Đây là kiểm tra bằng mock giao thức Dola, chưa phải tạo video Dola live.

Kết nối AI hiện có được đánh giá riêng bằng 8 mẫu Việt/Anh. Mẫu đơn giản được allow, thiếu tham chiếu/nhiều cảnh vượt thời lượng được review; rewrite giữ hành động và cấu trúc hai cảnh trong mẫu. Mẫu tiếng Anh vẫn thêm chi tiết nhỏ, nên cần duyệt đề xuất. Không lấy kiểm tra schema hoặc mock làm bằng chứng AI luôn đúng hay Dola sẽ chấp nhận.

Không thêm thư viện hay framework mới. Các chức năng tạo ảnh/asset, âm thanh, tự mua tài khoản và rotating proxy provider mới cần tích hợp riêng; giao diện không có control giả cho những phần này.

Nếu POST đã được nhận nhưng mất response, Studio tìm biên nhận theo tài khoản đăng nhập/mã yêu cầu trước khi gọi lại AI, kể cả sau reload hoặc token hết hạn. GET `/api/admin/video-receipts/{request_id}?kind=single|batch` chỉ đọc; chưa có biên nhận trả null. Prompt phân tích và gửi một video giữ cùng nguyên văn để neo gợi ý không lệch vì khoảng trắng.

Vai trò ảnh người dùng chọn được dùng cả khi tạo mẻ với ảnh chung. Log chuẩn và stdout của các thành phần chính cùng che URL/session/cookie/key; dữ liệu lịch sử gốc chỉ hiển thị ở chi tiết đã che secrets.

## Điều khiển mẻ và xử lý lỗi

Bấm **Xem mẻ** trên một video để mở mẻ cũ. **Tạm dừng lượt gửi mới** được lưu trong database, giữ qua restart; video Dola đã nhận vẫn được theo dõi và tải về. **Tiếp tục lượt gửi** mở lại hàng đợi. Chọn tối đa 50 video trong bảng/lưới để **Bỏ job chưa gửi** hoặc **Tải ZIP đã chọn**. Bỏ job không xóa dữ liệu và không hủy conversation đã gửi. ZIP có manifest ghi rõ file được tải và các job chưa có file.

**Duyệt / tạo lại lỗi đã chọn** chỉ nạp job có trạng thái thất bại đã xác nhận; bỏ qua video chưa rõ kết quả. Giữ prompt gốc, cho sửa trực tiếp, **AI đề xuất sửa**, hoàn tác và kiểm tra từng dòng. Chọn các dòng hợp lệ, xác nhận các lý do cần xem xét và xác nhận chi phí trước khi tạo video mới. Mỗi dòng tạo một video và lưu liên kết với job lỗi gốc. Không tự chạy lại hoặc tự dùng điểm khi AI đề xuất.

Bản nháp sửa lỗi và lịch sử được lưu trong trình duyệt; sau tải lại phải kiểm tra prompt lại vì token có hạn. Nếu phản hồi gửi bị mất, Studio giữ mã yêu cầu và tìm mẻ đã nhận trước khi kiểm tra/gửi lại, kể cả sau tải lại trang. Response AI cũ không ghi đè prompt đã chỉnh.

Job lỗi mới giữ ảnh gốc để tạo lại, theo thời hạn lưu task hiện có. Mỗi video tạo lại sở hữu bản sao ảnh riêng và giữ vai trò ảnh. Nếu ảnh gốc của job cũ đã bị xóa, Studio báo cần bổ sung ảnh; không thay bằng thumbnail.

Kiểm tra nút tạo trực tiếp riêng: `.venv/bin/python tests/run_direct_submit_journey.py /tmp/dola-direct-evidence`.

## Ngôn ngữ và các video đang chạy

Tool giữ nguyên nội dung prompt Việt/Anh, dùng chỉ dẫn tạo video và tham chiếu ảnh bằng tiếng Việt, yêu cầu Dola trả thông báo bằng tiếng Việt. Dola vẫn có thể trả nguyên văn tiếng Trung: thông báo chính diễn giải hạn mức cần/còn lại, lỗi không trừ lượt và thời gian dự kiến khi upstream cung cấp; nguyên văn đã che thông tin nhạy cảm nằm trong chi tiết kỹ thuật. Không dùng AI để dịch mỗi lượt polling.

Đổi model, thời lượng, tỉ lệ hoặc prompt chỉ thay bản nháp tiếp theo. Cấu hình của mỗi video được lưu khi gửi; tạo video mới hoặc xem tiến độ video khác không hủy video đang chạy. Khu vực tạo video hiển thị tất cả job đang chạy với cấu hình riêng, kể cả khi đã có nhiều video mới hơn trong lịch sử. Thanh tiến độ chờ và thông báo lấy từ job đã lưu; không suy ra phần trăm từ thời gian.
