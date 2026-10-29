<p align="center"><img src="web/logo.svg" width="112" height="112" alt="dola-pool logo"></p>
<h1 align="center">dola-pool</h1>
<p align="center">🌐 <a href="README.md">中文</a> | <b>Tiếng Việt</b></p>

Gom một nhóm tài khoản [dola.com](https://www.dola.com) (bản quốc tế của mô hình video Seedance của ByteDance) và [Doubao doubao.com](https://www.doubao.com) (bản Trung Quốc) thành một pool tài khoản, cung cấp ra ngoài **API tạo video / tạo ảnh tương thích OpenAI**, đồng thời tương thích giao thức dạng tác vụ của Volcano Ark (Ark), có thể kết nối trực tiếp vào các gateway như [new-api](https://github.com/QuantumNous/new-api).

- Pool nhiều tài khoản: tự chọn tài khoản, ghi sổ hạn mức, làm nguội khi dính Rủi ro, đổi tài khoản khi thất bại, reset hạn mức mỗi ngày
- **Dùng lẫn tài khoản của hai trang**: tài khoản dola nhận `seedance-2.0-fast` / `seedance-2.5`, tài khoản Doubao nhận video `doubao-seedance-*` và tạo ảnh `doubao-seedream-*`, tự phân phối theo tên mô hình, không lẫn tài khoản
- Hai đường tạo video: giao thức HTTP thuần (mặc định) và tự động hóa trình duyệt Chromium (dự phòng, chỉ dola)
- Tùy chọn cách ly Cookie nghiêm ngặt: mỗi tài khoản Cookie của Dola được gắn với một profile CloakBrowser cố định, fingerprint seed cố định và đường ra riêng
- Giao diện ra ngoài: kiểu OpenAI `/v1/videos`, `/v1/images/generations`, `/v1/images/edits`; kiểu Ark `/api/v3/contents/generations/tasks`
- Có sẵn trang quản trị: tài khoản, proxy, API Key của khách hàng, tác vụ, video, kiểm thử tải
- Tạo video từ ảnh tham chiếu / tạo ảnh từ ảnh, lưu lại cục bộ và phân phối tĩnh (tình trạng watermark xem mục «Lưu ý»)

> 📖 **Lần đầu sử dụng? Hãy đọc hướng dẫn sử dụng [`USAGE.vi.md`](USAGE.vi.md) trước**: các khái niệm cốt lõi, tác dụng của từng nút trong trang quản trị, tạo video đầu tiên từ con số 0, vận hành hằng ngày và câu hỏi thường gặp.
> 🍪 **Trước khi thêm tài khoản, hãy đọc [`COOKIE.vi.md`](COOKIE.vi.md)**: cách xuất Cookie của tài khoản dola / Doubao từ trình duyệt (tiện ích Cookie-Editor / công cụ dành cho nhà phát triển).
>
> Tài liệu tích hợp đầy đủ cho phía downstream (xác thực, trường dữ liệu, mã lỗi, API quản trị) xem tại [`API.md`](API.md).

## Các mô hình được hỗ trợ

| Mô hình | Độ phân giải | Thời lượng | Tư liệu tham chiếu | Số điểm trừ mỗi lần (mặc định mỗi tài khoản 4 điểm/ngày) |
|---|---|---|---|---|
| `seedance-2.5` | 720p | 5 / 10 / 30 giây (15 giây không tạo được, 30 giây cần bật `pair`, xem bên dưới) | Tối đa 10 ảnh; không hỗ trợ âm thanh, video tham chiếu | 5 giây = 2, 10 giây = 4, 15 giây = 12 (vượt hạn mức ngày), **30 giây = 2** (khi chỉ ra được nửa đầu và phải bù nửa sau thì tổng là 4) |
| `seedance-2.0-fast` | 720p | 5 / 10 / 15 giây | Tối đa 10 ảnh; không hỗ trợ âm thanh, video tham chiếu | 5 / 10 / 15 giây đều dùng 3 điểm |

- Số điểm trừ dựa theo **báo giá** trong phản hồi của dola, thay đổi theo «mô hình + thời lượng» (đo thực tế ngày 2026-10-14, xem `config.DURATION_COSTS`, có thể ghi đè bằng `DOLA_DURATION_COSTS`).
  Những tổ hợp có báo giá vượt hạn mức một ngày của một tài khoản (mặc định 4 điểm) sẽ bị trả về 422 ngay khi tiếp nhận: **15 giây của seedance-2.5 cần 12 điểm, tài khoản miễn phí không tạo được**, hãy dùng 10 giây, 30 giây hoặc seedance-2.0-fast.
- Một tài khoản mỗi ngày (4 điểm) tạo được: 2.5 —— 5 giây 2 video / 10 giây 1 video / 30 giây 2 video; 2.0 —— 5 / 10 / 15 giây: 1 video (còn 1 điểm).

- **30 giây**: từ 2026-10-12, dola trả lời các yêu cầu 30 giây là «chỉ hỗ trợ 4～15 giây, sẽ tạo theo 15 giây», nhưng **thực tế vẫn tạo video 30 giây** ([issue #4](https://github.com/Roins-hub/dola-pool/issues/4)). Hành vi do `DOLA_30S_MODE` quyết định:
  `reject` (mặc định, trả về 422 ngay khi tiếp nhận kèm lý do) / `pair` (**khuyến nghị**): **trước tiên gửi nguyên yêu cầu 30 giây một lần** —— dola ngoài miệng trả lời «sẽ tạo theo thời lượng hỗ trợ gần nhất là 15 giây, tiêu tốn 2», nhưng **thực tế đa số ra đủ 30 giây** (đo ngày 2026-10-14, cả 4 đơn đều 30.08 giây), chỉ trừ **2 điểm**; chỉ khi ra được nửa đầu (～15 giây) mới bù thêm một lần «nửa sau» rồi ghép cục bộ (trừ thêm 2 điểm). Số giây trong phản hồi không được tính, chỉ căn cứ vào thời lượng thực tế của file nhận về; khi dola hỏi lại «có tạo theo 15 giây không» thì tự động trả lời «xác nhận tạo»; chỉ phân cho tài khoản nhập bằng Cookie; khi có bù nửa sau thì hình ảnh giữa hai đoạn có thể không hoàn toàn liền mạch / `native` (logic cũ, gửi nguyên 30 giây; sau khi dola nói «chỉ hỗ trợ 4～15 giây» sẽ chờ thêm tối đa 10 phút, không khuyến nghị).
- Các thời lượng khác bị từ chối thẳng; yêu cầu đã ghi rõ 2.0 / 2.5 chỉ được kiểm tra theo đúng phiên bản đó, không lặng lẽ đổi mô hình.
- Video thành phẩm là 1280×720, 24fps, H.265, bitrate khoảng 1.5～4 Mbps (do upstream quyết định, hình ảnh càng đơn giản thì file càng nhỏ).
- `seedance-2.0-fast` chính là «Seedance 2.0 Fast» trên trang web dola (dola không có bản Seedance 2.0 tiêu chuẩn). Tên cũ `seedance-2.0` vẫn dùng được (tương thích client cũ và chuyển hướng mô hình của new-api), tự động xử lý như 2.0 Fast, nhưng không còn xuất hiện trong `/v1/models`.

### Mô hình cho tài khoản Doubao (doubao.com) (từ 2.3.0)

Pool tài khoản có thể chứa đồng thời tài khoản dola và tài khoản Doubao: **tài khoản Doubao chỉ nhận 4 mô hình bên dưới, tài khoản dola chỉ nhận `seedance-2.0-fast` / `seedance-2.5`**, không lẫn nhau.

| Mô hình | Tên trong Doubao | Thời lượng | Ai dùng được | Ghi chú tiêu hao trên trang Doubao | Đo thực tế (2026-10-18) |
|---|---|---|---|---|---|
| `doubao-seedance-2.0-mini` | Seedance 2.0 Mini | Số nguyên bất kỳ từ 4～15 giây | Tài khoản miễn phí, hội viên | «Dùng cho tạo hằng ngày», không ghi hệ số (tính là 1 lần) | Tài khoản miễn phí tạo liên tiếp 3 video 5 giây vẫn nằm trong hạn mức miễn phí; mỗi video mất khoảng 2 phút |
| `doubao-seedance-2.0-fast` | Seedance 2.0 Fast | Số nguyên bất kỳ từ 4～15 giây | Tài khoản miễn phí, hội viên | «Lựa chọn tạo video nhanh», không ghi hệ số | Chưa đo |
| `doubao-seedance-2.0` | Seedance 2.0 | Số nguyên bất kỳ từ 4～15 giây | **Chỉ hội viên** | «Thể hiện hình ảnh nâng cao · **tiêu hao gấp 2**» | Chưa đo; yêu cầu từ tài khoản miễn phí sẽ bị Doubao từ chối (pool tài khoản tự đổi sang tài khoản hội viên) |
| `doubao-seedance-2.5` | Seedance 2.5 | Số nguyên bất kỳ từ 4～**30** giây (gốc, không ghép) | **Chỉ hội viên** | «Chuẩn mực video hàng đầu · **tiêu hao gấp 5**» | Tài khoản gói trải nghiệm sau khi tạo 1 video 5 giây thì hạn mức miễn phí trong ngày đã hết, tạo tiếp Mini cũng báo phải dùng hạn mức trả phí |

> Doubao **không công khai hạn mức miễn phí mỗi ngày là bao nhiêu**, phản hồi cũng không báo phần còn lại, chỉ nói «lần tạo này sẽ tiêu tốn hạn mức miễn phí hằng ngày»; khi hết thì chuyển sang hỏi
> «lần này sẽ tiêu tốn hạn mức trả phí, có tiếp tục không?» (pool tài khoản xử lý theo `DOUBAO_ALLOW_PAID`). Vì vậy «hệ số» trong bảng trên là nguyên văn trên trang Doubao,
> tạo được bao nhiêu video thì căn cứ theo thực tế; quy tắc hạn mức Doubao điều chỉnh bất cứ lúc nào, theo đưa tin thì bản trải nghiệm miễn phí của 2.5 có thể bị hủy sau tháng 11.

- Tỷ lệ hỗ trợ `16:9 / 9:16 / 1:1 / 4:3 / 3:4 / 21:9`, không truyền thì Doubao tự quyết. Video thành phẩm 720p.
- **Watermark**: watermark hiển thị «AI tạo» của Doubao là cài đặt **theo từng tài khoản** (Cài đặt Doubao → Quản lý watermark AI tạo → Ảnh, video đã tạo).
  Khi `DOUBAO_AUTO_NO_WATERMARK=1` (mặc định), pool tài khoản đọc cài đặt này trước khi kiểm tra / tạo video, nếu chưa tắt thì dùng API cài đặt chính thức của Doubao để tắt giúp tài khoản,
  sau đó tải qua API tải xuống chính thức sẽ nhận được video thành phẩm sạch (đã thử thực tế với cả tài khoản miễn phí và hội viên); cột «Nguồn» trên trang quản trị hiển thị «Không watermark ✓ / ⚠ Chưa tắt watermark».
  Sau khi tắt watermark hiển thị, khi đăng công khai video, bên đăng phải tự ghi chú «AI tạo» (theo phần giải thích pháp lý trong trang cài đặt của Doubao).
- Doubao sẽ trả lời trước «vui lòng xác nhận tham số», pool tài khoản kiểm tra thời lượng và mô hình đều đúng thì mới tự động trả lời «xác nhận tạo»; không khớp thì không xác nhận (không trừ hạn mức) và báo lỗi ngay.
- Khi hết hạn mức miễn phí Doubao hỏi «lần này sẽ tiêu tốn hạn mức trả phí, có tiếp tục không?»: `DOUBAO_ALLOW_PAID=1` (mặc định) tự động xác nhận dùng trả phí;
  đặt thành `0` thì coi như tài khoản này hôm nay đã hết hạn mức và đổi tài khoản. Tài khoản không có hạn mức trả phí cũng đổi tài khoản tương tự.
- Khi tài khoản miễn phí yêu cầu 2.0 / 2.5, Doubao trả lời «hiện không thể dùng Seedance 2.5, có đổi sang Mini không»: pool tài khoản **tuyệt đối không xác nhận** (sẽ bị hạ cấp ngầm),
  đánh dấu tài khoản này là «tài khoản miễn phí» và phân lại cho tài khoản hội viên; tài khoản tạo video thành công với mô hình hội viên được đánh dấu là «hội viên», xem được ở cột «Nguồn» trên trang quản trị.
- Doubao không báo số video còn lại, trang quản trị hiển thị «Hôm nay đã tạo N video»; `DOUBAO_DAILY_LIMIT` (mặc định 999) chỉ là giới hạn trên dự phòng.
- Máy chủ ở nước ngoài dùng trực tiếp được (đã thử thực tế ngày 2026-10-18 với IP đầu ra Nhật Bản), không cần proxy Nhật Bản / Hàn Quốc.
- Chỉ hỗ trợ nhập bằng Cookie (xem mục «Tài khoản Doubao (doubao.com)» trong [COOKIE.vi.md](COOKIE.vi.md)). Các tên chính thức của plugin Doubao trong new-api như
  `doubao-seedance-2-0-260128` **không phải** mô hình của tài khoản Doubao, vẫn đi qua dola như cũ.

### Tạo ảnh (Seedream, từ 2.4.0, chỉ nhận tài khoản Doubao)

Tương thích OpenAI, **trả về đồng bộ**: `POST /v1/images/generations` (tạo ảnh từ văn bản), `POST /v1/images/edits` (tạo ảnh từ ảnh / sửa ảnh), chỉ phân cho tài khoản Doubao:

| Mô hình | Tên trên web Doubao | Tỷ lệ | Số ảnh mỗi lần |
|---|---|---|---|
| `doubao-seedream-5.0-flash` | Seedream 5.0 Flash (chuyên nghiệp, nhanh) | Tự động / 9:16 / 2:3 / 3:4 / 1:1 / 4:3 / 3:2 / 16:9 | 1 |
| `doubao-seedream-5.0-pro` | Seedream 5.0 Pro (tạo ảnh chuyên nghiệp · tiêu hao gấp 4) | Như trên | 1 |
| `doubao-seedream-4.5` | Seedream 4.5 (tạo hằng ngày) | Như trên | 1 |
| `doubao-seedream-4.0` | Seedream 4.0 (tạo ảnh cơ bản) | Như trên | 1 |

- Thân yêu cầu: `{"model", "prompt", "size"` (ví dụ 1536x1024, tự quy đổi sang tỷ lệ gần nhất) hoặc `"ratio"` (ví dụ 16:9),
  `"response_format": "url" | "b64_json", "reference_images": [URL ảnh / data URL]}`; edits cũng nhận file multipart `image` / `image[]`.
- Trả về: `{"created", "data": [{"url"} 或 {"b64_json"}], "model", "account", "unwatermarked"}` (`或` = hoặc). Độ phân giải mức 2K (16:9 đo thực tế 2720×1520), khoảng 20 giây ra ảnh.
- **Watermark**: dùng API tải xuống không watermark chính thức của Doubao; tài khoản đã tắt «watermark AI tạo» trong cài đặt Doubao thì ảnh sạch, pool tài khoản sẽ tự tắt giúp tài khoản (giống video, `DOUBAO_AUTO_NO_WATERMARK`).
- Doubao đọc tỷ lệ từ **văn bản**: pool tài khoản gửi theo định dạng `生成图片：<提示词>，16:9` (tạo ảnh: &lt;prompt&gt;, 16:9), nếu không Doubao sẽ ra ảnh 1:1.
- Toàn bộ yêu cầu chờ tối đa `DOLA_IMAGE_TIMEOUT` (mặc định 240 giây), trong thời gian đó thất bại thì tự đổi tài khoản; upstream từ chối tạo ảnh (kiểm duyệt) thì không đổi tài khoản mà trả về lý do ngay.
- Tạo ảnh không tính vào hạn mức video, không chọn tài khoản theo hạn mức (chỉ bỏ qua các tài khoản đang dính Rủi ro / chờ kích hoạt / bất thường / IP đầu ra đang bị giới hạn tần suất / đang tạo video).

## Điều phối và thời gian chờ

Mỗi tác vụ liên tục đổi tài khoản để tạo video trong **tổng thời hạn**, quá hạn thì bị coi là thất bại, không chờ vô hạn:

1. Chọn một tài khoản có thể điều phối và đang rảnh theo trọng số/độ ưu tiên để tạo;
2. Thất bại (không đủ hạn mức, chạm giới hạn ngày, Rủi ro, `generation_voided`, v.v.) thì tự đổi sang tài khoản tiếp theo;
3. Đã thử hết một vòng tài khoản mà vẫn chưa thành công thì chờ hết thời gian làm nguội sau thất bại rồi chạy lại một vòng từ đầu;
4. Quá `DOLA_TASK_DEADLINE` (mặc định **600 giây**) mà vẫn chưa thành công thì tác vụ thất bại và trả về
   `超过 600 秒仍未生成成功，任务失败（已尝试账号: …）` (quá 600 giây vẫn chưa tạo thành công, tác vụ thất bại (các tài khoản đã thử: …));
5. Khi toàn bộ pool đã chạm giới hạn ngày hoặc không đủ điểm thì trả về 429 ngay, không chờ suông;
6. Tác vụ đã có phiên (session), chỉ bị quá thời gian polling thì **không đổi tài khoản để gửi lại**, tránh việc phía Dola tạo video trùng và trừ hạn mức trùng.

## Triển khai một lệnh (khuyến nghị)

**Máy chủ Linux** (Ubuntu 20.04+ / Debian 11+), một lệnh cài đặt xong và đăng ký thành dịch vụ hệ thống:

```bash
# Truy cập trực tiếp bằng IP:8002
curl -fsSL https://github.com/Roins-hub/dola-pool/releases/latest/download/install.sh | sudo bash

# Có tên miền + HTTPS tự động (trỏ bản ghi A của tên miền về máy chủ này trước)
curl -fsSL https://github.com/Roins-hub/dola-pool/releases/latest/download/install.sh | sudo DOLA_DOMAIN=video.example.com bash
```

Cài xong sẽ in ra địa chỉ trang quản trị, tài khoản mặc định (`admin` / `admin123`, hãy đổi ngay) và API Key được sinh ngẫu nhiên.
Chạy lại cùng lệnh đó là nâng cấp, cấu hình và dữ liệu đều được giữ lại. Các tham số tùy chọn (thư mục cài đặt, cổng, tên dịch vụ, v.v.) xem phần chú thích ở đầu [`deploy/install.sh`](deploy/install.sh).

**Máy tính cá nhân**: tải gói mã nguồn trong [Release](https://github.com/Roins-hub/dola-pool/releases/latest) và giải nén, sau đó

- Windows: nhấp đúp `start-local.bat`
- macOS / Linux: `bash start-local.sh`

Lần chạy đầu tiên sẽ tự tạo môi trường ảo, cài phụ thuộc, tải Chromium, rồi mở `http://127.0.0.1:8002/`.
Cần cài sẵn Python 3.10+ và Node.js 18+. Nếu máy không ở Nhật Bản / Hàn Quốc thì cần cấu hình proxy (xem [`USAGE.vi.md`](USAGE.vi.md)).

### Triển khai bằng Docker

Image được phát hành tại `ghcr.io/roins-hub/dola-pool` (tự build theo từng tag phiên bản, `latest` là bản mới nhất). Cần Docker 20+ và Docker Compose:

```bash
mkdir dola-pool && cd dola-pool
curl -fsSLO https://raw.githubusercontent.com/Roins-hub/dola-pool/main/docker-compose.yml
curl -fsSL https://raw.githubusercontent.com/Roins-hub/dola-pool/main/deploy/dola.env.example -o dola.env
# Sửa dola.env: ít nhất phải đổi DOLA_ADMIN_KEY, DOLA_PUBLIC_BASE (địa chỉ truy cập từ bên ngoài, ví dụ http://IP-của-bạn:8000)
docker compose up -d
```

- Trang quản trị: `http://服务器IP:8000/` (IP máy chủ); kiểm tra sức khỏe `http://服务器IP:8000/health`. Cổng đổi trong `ports` của `docker-compose.yml`.
- **Mọi dữ liệu nằm trong `./data`** (tài khoản, cơ sở dữ liệu, video thành phẩm, ảnh tham chiếu), nâng cấp image không mất: `docker compose pull && docker compose up -d`.
- Muốn build từ mã cục bộ: thay `image:` trong `docker-compose.yml` bằng `build: .`, rồi chạy `docker compose up -d --build`.
- Image chỉ chạy **API thuần** (tài khoản dola / Doubao nhập bằng Cookie), không kèm trình duyệt, tài khoản đăng nhập bằng Google không dùng được trong Docker.
- Image khoảng 1.6 GB (Python 3.12 + Node + ffmpeg + opencv).

### Cách ly trình duyệt Cookie nghiêm ngặt (2.5.0)

Mặc định tài khoản Cookie vẫn đi qua HTTP thuần. Khi cần cố định tài khoản Cookie của Dola trong môi trường trình duyệt riêng, hãy triển khai bằng Linux
Xvfb/systemd và cài thêm phụ thuộc tùy chọn `deploy-cloak-requirements.txt`:

```env
DOLA_COOKIE_RUNTIME=cloak
DOLA_CLOAK_HEADLESS=0
CLOAKBROWSER_CACHE_DIR=/opt/dola-pool/.cloakbrowser
DOLA_ISOLATE_SHARED_EGRESS=1
DOLA_EGRESS_THROTTLE_ROTATE=0
DOLA_EGRESS_THROTTLE_MIN_ACCOUNTS=1
DOLA_MIN_SUBMIT_INTERVAL_SECONDS=15
```

Ở chế độ nghiêm ngặt, mỗi tài khoản Cookie của Dola dùng `accounts/<账号>/cloak-profile/` (`<账号>` = tài khoản), và được gắn với proxy cố định hoặc
sticky session riêng; xác minh, gửi yêu cầu và polling đều đi qua profile đó. Khi gặp `710022002` thì lùi lại (backoff) theo IP đầu ra và theo tài khoản, mặc định không
tự đổi IP. Khi nhập hàng loạt, hãy tích chọn «Bỏ qua xác thực trực tuyến» (跳过在线验证) trước, sau khi nhập thì gắn proxy riêng cho từng tài khoản, rồi lần lượt bấm «Xác thực» (验证).

Chế độ này không áp dụng cho image Docker API thuần hiện tại; các bước triển khai và di chuyển đầy đủ xem [`USAGE.vi.md`](USAGE.vi.md).

Nhánh chính đã sửa lỗi của v2.5.0: Cookie phiên bị mất ở lần khởi động thứ hai. Khi đóng trình duyệt, hệ thống lưu ảnh chụp phiên (session snapshot) riêng cho từng tài khoản,
lần sau chỉ khôi phục những Cookie phiên còn thiếu; sau khi đã đăng xuất thì không phát lại bản nhập ban đầu. Bản sửa này không có trong gói phát hành v2.5.0 gốc.

Đây là cách ly dữ liệu trình duyệt, không phải mỗi tài khoản một máy ảo riêng. Các tài khoản vẫn dùng chung máy chủ, tiến trình ứng dụng và cơ sở dữ liệu điều phối; tải video
vẫn dùng HTTP client riêng. Bản ghi proxy khác nhau hoặc sticky session khác nhau không đảm bảo IP công khai khác nhau, cần xác nhận bằng cách dò IP đầu ra thực tế.
`710022002` nghĩa là upstream từ chối / giới hạn tần suất, không thể chỉ dựa vào mã lỗi mà kết luận là do fingerprint hay IP, cũng không đảm bảo đổi trình duyệt là hết.
Chế độ 30 giây `pair` và tính năng lấy lại kết quả ở nền khi API thuần bị quá thời gian vẫn chỉ áp dụng cho tài khoản API thuần. Nhân CloakBrowser còn có giấy phép và giới hạn phiên bản / số phiên đồng thời riêng.

## Triển khai thủ công

Cần Python 3.10+. dola.com yêu cầu IP đầu ra Nhật Bản/Hàn Quốc, nếu máy chủ ở Trung Quốc đại lục thì bắt buộc phải cấu hình proxy.

```bash
git clone https://github.com/Roins-hub/dola-pool.git
cd dola-pool
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
patchright install chromium          # Cần cho đường dự phòng bằng trình duyệt

cp cookies.txt.example cookies.txt   # Điền cookie tài khoản, mỗi dòng một tài khoản (hoặc nhập trong trang quản trị sau khi khởi động)

export DOLA_API_KEYS=sk-your-key     # API Key ra ngoài, phân tách bằng dấu phẩy
export DOLA_ADMIN_KEY=change-me      # Khóa trang quản trị
export DOLA_PROXY=http://user:pass@jp-proxy:port
export DOLA_PUBLIC_BASE=https://your-domain   # Địa chỉ ra ngoài dùng để sinh video_url

# 4. Cấu hình dịch vụ
export DOLA_API_KEYS=sk-xxx        # API key ra ngoài, nhiều key phân tách bằng dấu phẩy
export DOLA_MAX_CONCURRENCY=0      # Số tạo video đồng thời toàn cục, 0 = không giới hạn (mặc định)
export DOLA_VIDEO_TIMEOUT=300      # Thời gian chờ tạo video (giây)

# 5. Khởi động
uvicorn server:app --host 127.0.0.1 --port 8002   # Chỉ lắng nghe trên máy cục bộ, nginx đảm nhận phía ngoài
```

Mở `http://127.0.0.1:8002/` để vào trang quản trị, `/docs` là tài liệu API Swagger.

### Triển khai bằng systemd

```ini
# /etc/systemd/system/dola-pool.service
[Unit]
Description=dola-pool OpenAI compatible video API
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/opt/dola-pool
EnvironmentFile=/opt/dola-pool/dola.env
ExecStart=/opt/dola-pool/.venv/bin/uvicorn server:app --host 127.0.0.1 --port 8002
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Ghi các biến môi trường vào `dola.env` (đã có trong `.gitignore`), sau đó chạy `sudo systemctl enable --now dola-pool`, rồi dùng nginx reverse proxy tới 8002 và cấu hình HTTPS.

## Ví dụ gọi API

```bash
# Tạo tác vụ
curl -X POST https://your-domain/v1/videos \
  -H "Authorization: Bearer sk-your-key" \
  -H "Content-Type: application/json" \
  -d '{"model":"seedance-2.5","prompt":"一只橘色小猫在草地上追逐蝴蝶","duration":10,"ratio":"16:9"}'
# -> {"id":"video_xxx","status":"queued",...}

# Truy vấn tác vụ: queued -> processing -> completed / failed
curl https://your-domain/v1/videos/video_xxx -H "Authorization: Bearer sk-your-key"

# Tải video thành phẩm
curl -o out.mp4 https://your-domain/v1/videos/video_xxx/content -H "Authorization: Bearer sk-your-key"
```

Ảnh tham chiếu có thể truyền URL công khai qua bất kỳ trường nào trong `reference_images`, `image_url`, `image`, `input_image(s)`; trường thời lượng tương thích `duration` / `seconds` / `duration_seconds`.

Video của tài khoản Doubao chỉ cần đổi mô hình thành `doubao-seedance-*` (thời lượng là số nguyên bất kỳ từ 4～15 giây, 2.5 lên tới 30 giây):

```bash
curl -X POST https://your-domain/v1/videos \
  -H "Authorization: Bearer sk-your-key" -H "Content-Type: application/json" \
  -d '{"model":"doubao-seedance-2.0-mini","prompt":"一只橘色小猫在草地上追逐蝴蝶","duration":7,"ratio":"9:16"}'
```

Tạo ảnh (chỉ nhận tài khoản Doubao, trả về đồng bộ):

```bash
curl -X POST https://your-domain/v1/images/generations \
  -H "Authorization: Bearer sk-your-key" -H "Content-Type: application/json" \
  -d '{"model":"doubao-seedream-5.0-flash","prompt":"雪地里的红色狐狸","size":"1536x1024"}'
# -> {"created":1792373000,"data":[{"url":"https://your-domain/videos/img_xxx.png"}],...}

# Tạo ảnh từ ảnh / sửa ảnh: tải ảnh lên bằng multipart (cũng có thể truyền URL qua reference_images trong JSON)
curl -X POST https://your-domain/v1/images/edits -H "Authorization: Bearer sk-your-key" \
  -F model=doubao-seedream-5.0-flash -F prompt="把猫的胡子去掉" -F image=@cat.jpg
```

### Kết nối với new-api

Dùng **plugin tác vụ Doubao** có sẵn trong new-api (loại kênh «Task Plugin / 任务插件», plugin chọn Doubao):

1. Địa chỉ API điền địa chỉ của dịch vụ này (ví dụ `https://your-domain`), khóa điền một key trong `DOLA_API_KEYS`;
2. Mô hình của kênh điền các tên chính thức mà plugin nhận biết: `doubao-seedance-2-0-260128`, `doubao-seedance-2-5-260628`;
3. Chuyển hướng mô hình:
   ```json
   {"doubao-seedance-2-0-260128": "seedance-2.0-fast", "doubao-seedance-2-5-260628": "seedance-2.5"}
   ```
4. Cấu hình giá cho hai mô hình này trong new-api (theo lượt hoặc theo lượng đều được).
5. Mô hình video của tài khoản Doubao làm tương tự: thêm các tên như `doubao-seedance-2.0-mini` vào kênh (hoặc đặt bí danh ra ngoài rồi dùng chuyển hướng mô hình để ánh xạ về);
   các tên chính thức của plugin new-api (`doubao-seedance-2-0-260128`, v.v.) **không** được phân cho tài khoản Doubao, vẫn xử lý theo dola.
6. Tạo ảnh đi qua API ảnh của OpenAI (`/v1/images/generations`), trong new-api chỉ cần kết nối mô hình `doubao-seedream-*` theo kênh OpenAI.

Plugin sẽ gọi `/api/v3/contents/generations/tasks` của dịch vụ này theo giao thức Ark. Nút «Kiểm tra» trên trang kênh của new-api không hỗ trợ kênh plugin tác vụ, việc hiển thị `Task Plugin channel test is not supported` là bình thường, hãy dùng yêu cầu thật để kiểm chứng.

## Cấu hình chính

| Biến môi trường | Mặc định | Giải thích |
|---|---|---|
| `DOLA_API_KEYS` | trống | API Key ra ngoài (phân tách bằng dấu phẩy); cũng có thể tạo Key kèm hạn mức trong trang quản trị |
| `DOLA_ADMIN_KEY` | trống | Khóa cho API quản trị / trang quản trị |
| `DOLA_PROXY` | `http://127.0.0.1:7890` | Proxy đầu ra để truy cập dola.com |
| `DOLA_PUBLIC_BASE` | `http://127.0.0.1:8000` | Địa chỉ ra ngoài dùng để sinh `video_url` |
| `DOLA_TASK_DEADLINE` | `600` | Tổng thời hạn của một tác vụ (giây), trong thời gian đó liên tục đổi tài khoản, quá hạn thì coi là thất bại |
| `DOLA_VIDEO_MAX_ATTEMPTS` | `0` | Số lần thử tối đa cho một tác vụ, `0` nghĩa là không giới hạn, chỉ bị ràng buộc bởi tổng thời hạn |
| `DOLA_GENERATION_FAILURE_RETRIES` | `2` | Sau khi đã gửi, nếu thẻ cuối của hội thoại xác nhận thất bại (trạng thái 5, không có video) thì đổi tài khoản gửi lại tối đa bấy nhiêu lần; từ chối nội dung không gửi lại; `0` để tắt |
| `DOLA_UPLOAD_RETRIES` | `3` | Số lần tải lại trang và tải ảnh tham chiếu lại trên cùng tài khoản khi tải lên thất bại (chưa gửi) |
| `DOLA_MAX_CONCURRENCY` | `3` | Số video tạo đồng thời toàn cục |
| `DOLA_MAX_PENDING_TASKS` | `100` | Giới hạn số tác vụ đang chờ xử lý, vượt quá thì trả về 429 |
| `DOLA_DAILY_LIMIT` | `4` | Số điểm hạn mức mỗi ngày của mỗi tài khoản |
| `DOLA_LIMIT_RESET_TZ` / `DOLA_LIMIT_RESET_HOUR` | `UTC` / `0` | Múi giờ và thời điểm reset hạn mức hằng ngày |
| `DOUBAO_ALLOW_PAID` | `1` | Khi tài khoản Doubao hết hạn mức miễn phí có tự xác nhận dùng hạn mức trả phí không; `0` = đổi tài khoản |
| `DOUBAO_DAILY_LIMIT` | `999` | Số video tối đa phân cho một tài khoản Doubao mỗi ngày (dự phòng; hạn mức thật xem phản hồi của Doubao) |
| `DOUBAO_POLL_INTERVAL` | `10` | Khoảng thời gian polling khi tài khoản Doubao tạo video (giây) |
| `DOLA_IMAGE_TIMEOUT` | `240` | Thời gian chờ tối đa cho một yêu cầu tạo ảnh (giây), trong thời gian đó thất bại thì đổi tài khoản |
| `DOUBAO_AUTO_NO_WATERMARK` | `1` | Khi tài khoản Doubao chưa tắt watermark hiển thị «AI tạo» thì tự tắt giúp; `0` = chỉ nhắc trên trang quản trị |
| `DOLA_PURE_API` | `1` | Ưu tiên dùng đường tạo video bằng HTTP thuần |
| `DOLA_PURE_FALLBACK_BROWSER` | `1` | Khi HTTP thuần thất bại thì quay về đường trình duyệt |
| `DOLA_COOKIE_RUNTIME` | `pure` | Runtime của tài khoản Cookie: `pure` = HTTP thuần; `cloak` = tài khoản Cookie của Dola cố định dùng profile CloakBrowser riêng, lưu lâu dài |
| `DOLA_COOKIE_PROFILE_SUBDIR` | `cloak-profile` | Thư mục con chứa profile trình duyệt riêng của từng tài khoản ở chế độ Cookie nghiêm ngặt |
| `DOLA_CLOAK_HEADLESS` | `0` | CloakBrowser có chạy headless không; môi trường production nên dùng chế độ có giao diện kèm Xvfb |
| `DOLA_CLOAK_GEOIP` | `0` | Có tự suy ra múi giờ/ngôn ngữ theo IP đầu ra của proxy không; khi dùng proxy cố định nên cấu hình cố định thủ công |
| `DOLA_ISOLATE_SHARED_EGRESS` | `0` | Có cấm nhiều tài khoản chạy đồng thời trên cùng một IP đầu ra không; ở chế độ Cookie nghiêm ngặt mặc định tự thành `1` |
| `DOLA_MIN_SUBMIT_INTERVAL_SECONDS` | `0` | Khoảng cách tối thiểu giữa hai lần gửi của cùng một tài khoản; ở chế độ Cookie nghiêm ngặt mặc định tự thành `15` |
| `DOLA_PURE_TIMEOUT` | `900` | Thời gian chờ polling cho một lần tạo video qua đường HTTP thuần (giây) |
| `DOLA_EXTENSION_ENABLED` | `1` | Nạp `extensions/dola30` (phụ thuộc cho thời lượng 30 giây và phân tích không watermark) |

Danh sách đầy đủ xem [`config.py`](config.py).

## Cấu trúc thư mục

| Đường dẫn | Tác dụng |
|---|---|
| `server.py` | Dịch vụ FastAPI: API cho khách hàng, API tương thích Ark, API quản trị, thực thi tác vụ |
| `browser_pool.py` | Điều phối pool tài khoản: chọn tài khoản, đổi tài khoản thử lại, tổng thời hạn, hạn mức và làm nguội |
| `pool.py` / `store.py` / `user_store.py` | Pool tài khoản, lưu trữ tác vụ (SQLite), quản trị viên |
| `proxy_store.py` | Quản lý và xoay vòng proxy tĩnh / động |
| `pure_api_gen.py` / `protocol/` | Hiện thực giao thức tạo video bằng HTTP thuần (dola) |
| `protocol/doubao_site.py` / `sites.py` | Cấu hình trang Doubao (dùng lại cùng một bộ mã giao thức), nhận diện trang của tài khoản và mô hình |
| `doubao_gen.py` | Tài khoản Doubao tạo video: xử lý các câu thoại xác nhận / trả phí / mô hình hội viên, tự tắt watermark AI, tải xuống không watermark |
| `image_gen.py` | Tài khoản Doubao tạo ảnh (Seedream) |
| `browser.py` / `video_worker_ui.py` | Đường tạo video bằng tự động hóa trình duyệt |
| `media.py` | Tải ảnh tham chiếu, siêu dữ liệu của video thành phẩm |
| `web/` | Frontend của trang quản trị |
| `web/i18n/` | Bản dịch giao diện quản trị: `i18n.js` + `<lang>.json`; xem `I18N.md` |
| `i18n_tool.py` | Công cụ trích / đồng bộ / kiểm tra bản dịch |
| `extensions/dola30/` | Tiện ích mở rộng Chromium của bên thứ ba (thời lượng 30 giây, phân tích không watermark) |
| `deploy-cloak-requirements.txt` | Phụ thuộc tùy chọn của CloakBrowser wrapper cho chế độ Cookie nghiêm ngặt |
| `Dockerfile` / `docker-compose.yml` / `docker/` | Image Docker và cấu hình điều phối container (API thuần, dữ liệu nằm trong `/data`) |
| `.github/workflows/` | Tự chạy kiểm thử khi push (`tests.yml`); build và phát hành image khi gắn tag phiên bản (`docker.yml`) |
| `tests/` | Kiểm thử pytest |

Chạy kiểm thử:

```bash
pip install pytest
pytest -q tests
```

## Lưu ý

- Cookie của tài khoản hết hạn sau khoảng 60 ngày; tài khoản mất trạng thái đăng nhập sẽ được tự động đánh dấu và cần nhập lại. Tài khoản đã nhập vào pool **đừng bấm đăng xuất trong trình duyệt** (đo thực tế với Doubao: đăng xuất xong Cookie mất hiệu lực ngay).
- **Watermark**:
  - **Video dola**: địa chỉ tải mặc định mà web dola cung cấp có watermark (`lr=cici_ai`), pool tài khoản tải một phiên bản khác của cùng video đó trong dữ liệu dola trả về (`lr=unwatermarked`), không xử lý gì trên hình ảnh. API phân tích của bên thứ ba (`NOWATERMARK_API_ENDPOINT` trong `protocol/dola_pure_api.py`) **mặc định tắt** (`DOLA_PURE_REMOVE_WATERMARK=0`); chỉ khi đặt thành `1` mới được bật, lúc đó thông tin phát video sẽ được gửi qua HTTP cho bên thứ ba. Nguyên lý và rủi ro xem chi tiết tại [mục «Về việc xóa watermark» trong `USAGE.vi.md`](USAGE.vi.md#7-về-việc-xóa-watermark).
  - **Video / ảnh Doubao**: watermark hiển thị «AI tạo» là cài đặt chính thức theo từng tài khoản của Doubao (Cài đặt Doubao → Quản lý watermark AI tạo). Khi `DOUBAO_AUTO_NO_WATERMARK=1` (mặc định), pool tài khoản dùng API cài đặt chính thức để tắt giúp tài khoản, rồi dùng API tải xuống không watermark chính thức; sau khi tắt watermark hiển thị, khi đăng công khai nội dung, bên đăng phải tự ghi chú «AI tạo».
- `accounts/`, `*.db`, `dola.env`, `cookies.txt`, `downloads/` đều đã được bỏ qua (ignore), tuyệt đối không commit thông tin xác thực thật.

## Miễn trừ trách nhiệm

Kho mã này là ghi chép cá nhân phục vụ học tập và nghiên cứu khả năng tương tác, có chứa phần hiện thực dịch ngược các giao diện web của upstream; mã nguồn không chứa bất kỳ tài khoản, cookie, khóa hay thông tin xác thực nào có thể sử dụng. `extensions/dola30/` là tiện ích mở rộng của bên thứ ba, quyền cấp phép và quyền sử dụng thuộc về tác giả gốc. Người sử dụng phải tự tuân thủ điều khoản dịch vụ của dola.com, doubao.com và pháp luật nơi mình sinh sống (bao gồm các yêu cầu về ghi nhãn nội dung do trí tuệ nhân tạo tạo ra), tự đánh giá rủi ro về tài khoản và tuân thủ.

Trang quản trị hỗ trợ xuất Cookie dưới dạng JSON / ZIP và chọn proxy trước khi thêm tài khoản; thêm hàng loạt có thể chọn nhiều proxy để phân lần lượt. Xem USAGE.vi.md.
