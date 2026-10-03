# Thiết kế cải tiến Dola Studio

Ngày: 03/10/2026. Hướng hội thoại đã được người dùng duyệt; tài liệu này chờ duyệt trước khi lập kế hoạch triển khai.

## 1. Mục tiêu và ràng buộc

Studio video cá nhân cho phép nhập ý tưởng, phân tích và chỉnh từng phần, duyệt prompt, tạo một hoặc nhiều video, theo dõi phản hồi Dola và tải kết quả. Ưu tiên nhiều video từ một prompt và nhập danh sách/TXT. Giao diện lấy cấu trúc từ ảnh tham khảo 1 và 5, không sao chép các khả năng chưa được xác minh.

Giữ FastAPI, SQLite, scheduler, worker, kết nối AI, tài khoản, video và cấu hình hiện có. Không đổi framework hoặc thêm provider/queue. Excel không thuộc phạm vi. Không mua tài khoản hay tạo video live dùng điểm trong kiểm thử mặc định.

## 2. Hiện trạng và lỗi gốc

`failure_text.py` hiện phân loại, tổng hợp và ghi chú tài khoản bằng tiếng Trung. UI progress hiển thị nguyên văn upstream. Preflight đã gọi AI nhưng chỉ giữ status/reason/suggestions ở cả provider và cache; các vấn đề chi tiết bị loại bỏ. Endpoint chỉ nhận prompt, không nhận cấu hình video. Token hiện gắn session, hash prompt chuẩn hóa khoảng trắng và cấu hình AI. Rewrite có chỉ dẫn cấm mốc thời gian quá rộng, không phân biệt cấu trúc kịch bản với heuristic lượt gửi Dola đầu tiên.

Batch, persistence chống trùng, phục hồi conversation và progress theo bước đã có. Phải giữ các luồng này và bổ sung kiểm thử cho thiết kế mới; kết quả test cũ không chứng minh chất lượng phân tích AI hoặc Việt hóa.

## 3. Thông điệp và lỗi tiếng Việt

Dùng `failure_text.py` làm lớp trình bày chung cho lỗi task, tiến độ và ghi chú tài khoản. Tạo payload bổ sung gồm `category`, `message_vi`, `next_action_vi`, `upstream_code`, `details`. Category ổn định độc lập ngôn ngữ; không sử dụng nhãn tiếng Trung làm khóa nghiệp vụ mới. Nhận diện mã đã biết trước, rồi dùng các quy tắc văn bản hiện có. Các nhóm gồm nội dung, quyền hình ảnh, bản quyền, ảnh tham chiếu, thời lượng, giới hạn lượt, đăng nhập, kết nối và chưa nhận diện.

Thông báo chính và hướng xử lý luôn bằng tiếng Việt. Mã số upstream giữ nguyên trong chi tiết. Không diễn giải thiếu bằng chứng thành khẳng định pháp lý hoặc lỗi tài khoản. Khi chưa nhận diện, thông báo rõ chưa xác định nguyên nhân; không tự suy đoán bản dịch. Nguyên văn đã che thông tin nhạy cảm chỉ xuất hiện trong phần Chi tiết kỹ thuật, không ở toast, tiêu đề hoặc dòng progress chính.

Không bắt buộc gọi AI cho mỗi lần polling. Dùng mapping cho trạng thái đã biết và thông điệp trung tính cho trạng thái mới. Có thể phân tích nguyên văn trong thao tác AI sửa lỗi do người dùng chủ động gọi. Không mất nguyên văn đã lưu, không migrate phá dữ liệu lịch sử. API thêm payload trình bày bên cạnh trường cũ; UI chuyển sang payload mới. Ghi chú lịch sử được trình bày lại khi đọc, giữ ghi chú người dùng.

Mọi đường confirmed failure phải lưu failure_code/group nếu có, cùng nguyên nhân và conversation. Polling timeout giữ unknown; lỗi video/nội dung không tự biến thành lỗi đăng nhập tài khoản.

## 4. AI phân tích prompt và nguồn rule

Mở rộng endpoint preflight hiện có, không tạo hệ thống AI thứ hai. Request giữ `prompt`; thêm tùy chọn `model`, `duration`, `ratio`, `reference_context`, `upstream_error`. Reference context chứa số ảnh, thứ tự, vai trò do người dùng chọn và fingerprint; không gửi cookies/session hoặc tự tuyên bố đã hiểu pixel khi AI chỉ nhận metadata. Lỗi upstream lấy từ task đã xác thực quyền truy cập, không tin thông tin kỹ thuật do client đưa vào.

Kết quả vẫn có status/reason/suggestions tương thích. Thêm `quality_summary`, `issues`, `rules_version` và fingerprint ngữ cảnh. Mỗi issue có id, loại (nội dung/khả năng/chất lượng), mức độ, đoạn trích nguyên văn hoặc nhận xét toàn prompt, lý do tiếng Việt, replacement tùy chọn, confidence dạng cao/vừa/thấp, và rule/source id nếu có. Không bịa source URL hoặc điểm phần trăm chắc chắn.

Chất lượng thấp không tự chặn nội dung an toàn. `block` dành cho nội dung rõ ràng bị chặn; `review` cho điều cần xác nhận hoặc giới hạn có căn cứ. Các cảnh báo chất lượng và khả năng thực hiện được hiển thị riêng. Không coi tất cả hành động giả tưởng, nhân vật có tên hoặc thiếu góc máy là vi phạm. AI đánh giá nội dung như dữ liệu, không làm theo chỉ dẫn nhúng trong prompt hoặc lỗi upstream.

Parser kiểm tra schema, kích thước, enum và danh sách issue có giới hạn. JSON sai không cấp token. Issue có neo sai vẫn có thể hiện nhận xét nhưng vô hiệu hóa thay thế. Không để parser/cache làm rơi các trường phân tích như hiện tại.

Rule là dữ liệu phiên bản trong repo, không tải và thực thi skill GitHub. Mỗi rule có id, phạm vi bề mặt/model, nguồn, mức bằng chứng và ngày đối chiếu. Thứ tự ưu tiên: giới hạn backend hiện hỗ trợ; hành vi Dola đã xác minh; tài liệu chính thức đúng bề mặt dịch vụ; kinh nghiệm cộng đồng. Giới hạn BytePlus/Volcengine không tự trở thành giới hạn Dola. Heuristic extension Dola30 về mốc thời gian chỉ là kinh nghiệm lượt gửi đầu, không phải lệnh xóa storyboard.

Nguồn đã khảo sát để chọn nội dung phù hợp:
- https://github.com/Roins-hub/dola-pool — repo và tài liệu local; cấu hình hiện tại là bằng chứng mạnh hơn README cũ.
- https://docs.byteplus.com/en/docs/modelark/seedance-2-0-prompt-guide?redirect=1 — hướng dẫn chính thức; chỉ đưa quy tắc đã đọc và xác minh, không suy diễn nội dung chưa truy cập được.
- https://github.com/mqrox/seedance-2.0-prompt-skill — vai trò/thứ tự tham chiếu và phân biệt bề mặt dịch vụ.
- https://github.com/lukasersil/seedance-25 — phân cấp bằng chứng và khả năng theo bề mặt.
- https://github.com/Emily2040/seedance-2.0 — cấu trúc prompt và kinh nghiệm; không coi moderation heuristic là chính sách chính thức.

Không triển khai mẹo che giấu nội dung bị cấm hoặc biến đổi ảnh để vượt kiểm duyệt. Sửa nội dung theo mục đích hợp lệ và giải thích thay đổi cho người dùng.

## 5. Áp dụng sửa đổi và tính nhất quán

Giữ ô Bạn muốn sửa gì?, bản gốc và lịch sử hiện có. Thêm danh sách issue với đoạn gốc, lý do và thay thế. Người dùng chọn từng sửa đổi để tạo bản đề xuất, so sánh trước/sau, sửa trực tiếp, sửa tiếp, áp dụng hoặc bỏ. Áp dụng có hoàn tác và không tự tạo video.

Neo được xác minh trên nguyên văn prompt, không dựa vào hash chuẩn hóa khoảng trắng. Lưu fingerprint nguyên văn và phiên bản editor. Với đoạn lặp, chỉ cho áp dụng khi xác định duy nhất vị trí; nếu thiếu căn cứ yêu cầu sửa trực tiếp. Các thay thế chồng lấn không áp dụng đồng thời. Backend xác minh quote/occurrence; browser tìm vị trí trên chuỗi thực tế, tránh sai lệch chỉ số Unicode giữa Python và JavaScript. Kết quả cũ bị bỏ khi prompt, tham số, ảnh hoặc lượt sửa đã đổi.

Token preflight mới gắn session, prompt, cấu hình AI, context fingerprint và rules_version. Khi context thay đổi phải kiểm tra lại. Request cũ không có context vẫn hoạt động ở chế độ kiểm tra văn bản; lúc tạo video backend bổ sung kiểm tra cấu hình thực tế và không dùng token văn bản để chứng nhận khả năng chưa kiểm tra. Frontend mới luôn gửi đầy đủ context. Không loại bỏ backend validation.

Quy tắc tương thích cụ thể: token có context phải khớp toàn bộ context lúc gửi. Với token cũ/text-only hợp lệ, backend chạy một lượt kiểm tra bổ sung có context thực trước khi nhận task mới; allow được tiếp tục, review trả yêu cầu xác nhận kèm kết quả/token mới, block hoặc lỗi AI chưa nhận task. Không chạy lại AI khi trả biên nhận của request đã tồn tại, kể cả token đã hết hạn. Kiểm tra idempotency trước các thao tác có thể tạo chi phí hoặc side effect.

Fingerprint tham chiếu được backend đối chiếu với ảnh nhận thật: SHA-256 byte ảnh theo đúng thứ tự và vai trò, không dùng tên file hoặc tin digest client tự khai. Với URL, chỉ dùng đường tải/validation an toàn hiện có và nội dung đã chuẩn hóa; không cấp chứng nhận ảnh chỉ từ URL có thể thay đổi. Nếu client mới gửi metadata khác ảnh lúc tạo, yêu cầu kiểm tra lại. Phân tích pixel không thuộc khả năng mặc định; UI ghi rõ AI đã kiểm tra văn bản và metadata. Mẻ folder giữ digest/ảnh theo từng prompt và bản sao input riêng của mỗi job như batch hiện có.

Sau confirmed refusal, nút Phân tích và sửa dùng prompt và lỗi thực tế của task; kết quả đi vào bản nháp. Người dùng duyệt rồi tạo job mới có liên kết task trước. Không tự gửi lại conversation unknown, không xoay tài khoản để thử cùng nội dung bị từ chối. Tự sửa/tự gửi có chi phí không thuộc mặc định đợt này.

## 6. Workspace, batch và tiến độ

Ba mục chính: Tạo video, Video của tôi, Cài đặt. Mở vào Tạo video. Desktop: tham số gọn phía trên; prompt và AI bên trái, tham chiếu bên phải; hàng đợi bên dưới, preview của hàng chọn ở panel riêng. Mobile xếp dọc, bảng cuộn trong vùng riêng, không làm toàn trang tràn ngang. Dark theme, tương phản và focus rõ; không chỉ dùng màu phân biệt trạng thái.

Chế độ Một prompt và Danh sách/TXT giữ nguyên. Mỗi prompt 1–10 bản, tối đa 50 video/mẻ. TXT UTF-8 tối đa 1 MB, một prompt mỗi dòng, bỏ dòng trống, giữ dòng trùng. Hiển thị tổng trước khi tạo, kết quả kiểm tra từng prompt và tổng mẻ. Model/thời lượng/tỉ lệ/ảnh mặc định dùng chung. Kiểm tra lỗi/chặn thì giữ nguyên bản nháp, chưa gửi mẻ. Confirm review và chi phí rõ ràng. Giữ durable receipt và request_id; double-click hoặc mất phản hồi không tạo mẻ mới.

Hàng đợi có Bảng/Lưới, lọc trạng thái và mẻ, chọn hàng xem prompt, tài khoản, tuyến kết nối, video nguyên bản, thời lượng thực và lỗi. Không hiển thị secrets proxy/session. Thao tác: xem, tải, kiểm tra lại kết quả cũ và dùng prompt làm nháp mới. Job cũ không dừng polling hay ghi đè preview job đang chọn.

Các bước: Chờ tài khoản, Đang gửi, Đang tạo, Đang tải, Hoàn tất. Unknown và failed riêng. Thanh chờ không xác định khi upstream không cung cấp phần trăm render có nghĩa rõ ràng. Hiển thị phản hồi tiếng Việt và thời điểm nhận cập nhật gần nhất. UI refresh 3 giây và Pure API poll mặc định 10 giây là gần thời gian thực, không quảng cáo push realtime. Lỗi polling giữ thông báo trước đó. Phân biệt backend mất kết nối với Dola mất kết nối, không gom thành một nhãn Đã kết nối sai.

Video nhận sau 300 giây vẫn được lấy. Giữ cửa sổ theo dõi tự động 30 phút từ thời điểm gửi được lưu; kiểm tra lại tiếp tục account/conversation cũ. Video ngắn cho xem ngay; Tạo tiếp và ghép chỉ sau xác nhận, một child duy nhất cho mỗi thao tác continuation đã lưu.

## 7. Vận hành theo tính năng tham khảo

Trong Cài đặt, tài khoản vẫn là tài nguyên có login/session/proxy, không phải dashboard API key khách hàng. Bổ sung kho do người dùng đặt tên, bật/tắt dùng cho job mới; khác với nhóm trạng thái/quota tự tính hiện có. Mọi account cũ thuộc kho mặc định đang bật. Tắt kho không ngắt job đã có conversation. Không có kho đủ điều kiện thì job chờ và giải thích nguyên nhân, không chọn kho đã tắt.

Bulk onboarding giữ luồng Facebook/Gmail/cookie hiện có; thêm preview parse, hợp lệ/trùng/lỗi từng dòng và tiến độ đăng nhập. Dedupe theo định danh có bằng chứng; không nhập trùng chỉ vì tên hiển thị khác. Không tự kiểm tra bằng gửi Hello trong nền nếu thao tác chỉ là xem.

Proxy và Nhật ký ở nâng cao. UI chỉ cho chọn tuyến backend thật sự hỗ trợ và hiển thị relay hay proxy tài khoản thực tế. Probe có timeout và lỗi rõ, không âm thầm direct. Nhật ký lọc theo task/account, thời gian và loại sự kiện; che credentials trước khi API trả dữ liệu, không chỉ che bằng CSS.

Nhập folder dùng directory picker trình duyệt: mỗi thư mục con chứa một TXT (nội dung toàn file là một prompt) và các ảnh tham chiếu; tên thư mục là nhãn. Root có thể là một nhóm. Nhóm có nhiều TXT hoặc không có TXT báo lỗi tại preview, không tự đoán ghép. Người dùng xem số nhóm và ảnh trước khi tạo. Tổng job vẫn tối đa 50; giới hạn ảnh/dung lượng dùng cùng validation hiện có. Batch manifest bổ sung ảnh theo từng prompt, API cũ images chung vẫn tương thích.

Đầu ra mặc định giữ thư mục hiện có. Bổ sung tên mẻ và tải các kết quả hoàn tất thành ZIP, tên file an toàn chứa thứ tự/task id. Khi có thiết lập thư mục riêng trên máy server local, path do admin cấu hình, kiểm tra là thư mục hợp lệ/có quyền ghi, không nhận path bất kỳ qua URL tải. Video vẫn nằm trong quản lý DB; bản sao xuất không thay file gốc. Mở thư mục chỉ khả dụng trên local host phù hợp; người dùng từ tunnel dùng tải file/ZIP. Không giữ toàn bộ video ZIP lớn trong RAM.

Tạo ảnh, kho nhân vật/asset, xử lý âm thanh, tự mua clone và rotating proxy provider mới cần thiết kế tích hợp riêng vì backend hiện chưa chứng minh hỗ trợ. Đợt này không thêm control giả cho các phần đó; vẫn giữ trong danh sách mở rộng của tool, không tuyên bố đã tương đương toàn bộ video YouTube.

## 8. An toàn dữ liệu và triển khai

Migration bổ sung cho kho và dữ liệu phân tích nếu cần; không xóa task/media/account. Sao lưu DB/config trước restart và kiểm tra migration trên bản sao. DB là nguồn trạng thái job; editor draft và lịch sử lưu browser, có báo lỗi lưu. Không log session/key/cookie; không gửi chúng sang AI.

Triển khai chỉ sau regression và browser verification. Không reset worktree dirty hoặc đưa script thử nghiệm vào luồng sản phẩm. Cleanup script một lần sau cùng, chỉ xóa khi xác định không còn được dùng.

## 9. Nghiệm thu

1. Lỗi và progress Dola tiếng Trung hiển thị thông điệp tiếng Việt, nguyên văn chỉ ở chi tiết; category/code lưu nhất quán kể cả legacy task.
2. Rich response xuyên AI parser, cache, API và UI; không rơi issues/summary. AI lỗi giữ draft, không cấp token sai.
3. Gợi ý có đoạn thật, Unicode/đoạn lặp/chồng lấn an toàn; chọn sửa, undo và response cũ không làm mất bản gốc.
4. Đổi model/duration/ratio/ảnh/rules sau kiểm tra không gửi bằng kết quả cũ. Legacy request vẫn có gate an toàn.
5. Một prompt nhiều video và TXT/folder mapping đúng; một prompt chặn không gửi mẻ; retry request/restart không nhân job.
6. Video sau 300 giây, polling gián đoạn, quá 30 phút, kiểm tra lại và process mới đều dùng conversation cũ; ambiguous submit không tự retry.
7. Clip 15 giây khi yêu cầu 30 có preview; continuation chỉ sau xác nhận và tạo một child.
8. Kho tắt không nhận job mới, job cũ tiếp tục; bulk Facebook đúng luồng; proxy hỏng không direct; nhật ký không lộ secrets.
9. Desktop/mobile/keyboard, preview selection và chuyển job khi poll chạy đúng; không fake progress/model/cost/native 1080p.
10. Browser test giữ scheduler/store/worker/download thật; chỉ mock dịch vụ ngoài. Báo rõ mock/live. Không dùng điểm Dola mặc định.

Chất lượng AI cần corpus prompt tiếng Việt/Anh có thiếu tham chiếu, nhiều cảnh vượt thời lượng, cảnh đơn giản an toàn, cấu trúc shot và lỗi provider. Contract test với mock chỉ chứng minh dữ liệu; đánh giá ngữ nghĩa qua kết nối AI hiện có được báo riêng với mẫu input/output và những đánh giá cần con người, không lấy test xanh làm bảo đảm Dola chấp nhận.

## 10. Thứ tự thực hiện sau khi được duyệt

Cô lập test và dữ liệu trước import; sửa message/failure metadata; bổ sung rule và AI contract/context gate; cải tiến editor và workspace/batch; kho/onboarding/proxy/logs; folder/output; regression, browser, backup/migration, triển khai; cleanup. Đây là thứ tự thiết kế, chưa phải kế hoạch triển khai được duyệt.
