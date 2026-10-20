> 🌐 [中文](USAGE.md) | **Tiếng Việt**

# Hướng dẫn sử dụng dola-pool

> Tài liệu này dành cho **người lần đầu làm quen với dola-pool**: nó là gì, từng nút trong bảng quản trị dùng để làm gì, thao tác hằng ngày ra sao, gặp sự cố thì xem ở đâu.
>
> - Muốn **gọi API để tạo video** (viết code tích hợp) → xem [`API.md`](API.md)
> - Muốn **triển khai lên máy chủ / chạy cục bộ** → xem mục «Triển khai một lệnh» trong [`README.vi.md`](README.vi.md) (một lệnh là cài xong)
> - Muốn **sửa code** → xem [`DEVELOPMENT.md`](DEVELOPMENT.md)

> Ghi chú: bảng quản trị có thể chuyển sang tiếng Việt. Khi tài liệu nhắc tới một nút / nhãn trên bảng quản trị, lần đầu xuất hiện trong mỗi mục sẽ ghi nhãn tiếng Việt kèm nhãn tiếng Trung gốc trong ngoặc, ví dụ «Kiểm tra tải (压力测试)», để dù dùng giao diện ngôn ngữ nào bạn cũng tìm được.

---

## Mục lục

1. [Hiểu dola-pool trong một câu](#1-hiểu-dola-pool-trong-một-câu)
2. [Nắm trước 6 khái niệm](#2-nắm-trước-6-khái-niệm)
3. [Bắt đầu lần đầu: từ con số 0 đến video đầu tiên](#3-bắt-đầu-lần-đầu-từ-con-số-0-đến-video-đầu-tiên)
4. [Giải thích từng trang của bảng quản trị](#4-giải-thích-từng-trang-của-bảng-quản-trị)
5. [Bên gọi API sử dụng thế nào (bản tối giản)](#5-bên-gọi-api-sử-dụng-thế-nào-bản-tối-giản)
6. [Vận hành hằng ngày](#6-vận-hành-hằng-ngày)
7. [Về việc xóa watermark](#7-về-việc-xóa-watermark)
8. [Câu hỏi thường gặp](#8-câu-hỏi-thường-gặp)
9. [Bảng tra nhanh](#9-bảng-tra-nhanh)

---

## 1. Hiểu dola-pool trong một câu

**Gom nhiều tài khoản dola.com thành một “pool tài khoản”, rồi đưa ra ngoài thành một API tạo video thống nhất.**

```
 Bên gọi (website của bạn / new-api / công cụ canvas)
          │  POST /v1/videos  (kèm API Key)
          ▼
 ┌──────────────────────────────────────────────┐
 │                  dola-pool                   │
 │  ① Nhận tác vụ, xếp hàng                     │
 │  ② Chọn từ pool một tài khoản còn hạn mức    │
 │  ③ Dùng tài khoản đó lên dola.com tạo video  │
 │  ④ Thất bại thì tự đổi tài khoản thử lại     │
 │  ⑤ Tải video về máy, trả về link ổn định     │
 └──────────────────────────────────────────────┘
          │  qua proxy Nhật Bản / Hàn Quốc
          ▼
     dola.com (mô hình video Seedance của ByteDance)
```

Mỗi tài khoản dola chỉ có rất ít hạn mức miễn phí mỗi ngày (mặc định 4 điểm, khoảng 1～2 video). Giá trị của dola-pool là: **càng nhiều tài khoản, mỗi ngày tạo được càng nhiều video**, còn bên gọi hoàn toàn không cần quan tâm phía sau là tài khoản nào.

---

## 2. Nắm trước 6 khái niệm

### 2.1 Tài khoản (pool tài khoản)

Một tài khoản dola.com = một “tài khoản” trong pool, trên bảng quản trị được gọi là `acc1`, `acc2`……

Có hai cách thêm tài khoản:

| Cách | Cần gì | Phù hợp khi |
|---|---|---|
| **Đăng nhập Google** | Email Google + mật khẩu + khóa bí mật xác minh hai bước TOTP | Bạn có tài khoản Google, để máy chủ tự động đăng nhập |
| **Nhập Cookie** | Cookie xuất ra từ trình duyệt sau khi đã đăng nhập dola.com | Bạn đã đăng nhập sẵn tài khoản trên máy tính của mình |

> Cookie hết hạn sau khoảng **60 ngày**, khi đó tài khoản sẽ bị đánh dấu là phiên đăng nhập hết hiệu lực và cần nhập lại.

### 2.2 Hạn mức (điểm)

Mỗi tài khoản mỗi ngày có **4 điểm** hạn mức (`DOLA_DAILY_LIMIT`), chỉ **tạo video thành công** mới bị trừ điểm:

| Mô hình | Thời lượng → điểm trừ mỗi video | Một tài khoản một ngày (4 điểm) tạo được |
|---|---|---|
| `seedance-2.5` | 5 giây = 2 điểm, 10 giây = 4 điểm, **15 giây = 12 điểm (không tạo được)**, **30 giây = 2 điểm** (cần bật `pair`; nếu chỉ ra nửa đầu và phải bù nửa sau thì tổng cộng 4 điểm) | 2 video 5 giây, 1 video 10 giây, hoặc 2 video 30 giây |
| `seedance-2.0` | 5 giây = 1 điểm, 10 giây = 2 điểm, 15 giây ≈ 3 điểm (ước tính, chưa đo thực tế) | 4 video 5 giây, 2 video 10 giây, hoặc 1 video 15 giây |

- Điểm bị trừ **theo «mô hình + thời lượng»**, khớp với mức giá trong câu trả lời của dola «将消耗 N 个视频生成额度» (sẽ tiêu tốn N hạn mức tạo video) (đo thực tế 2026-10-14).
- Tổ hợp có giá vượt quá hạn mức một ngày của một tài khoản (ví dụ 15 giây của 2.5) sẽ bị từ chối ngay khi gửi, kèm lý do; muốn 15 giây hãy dùng seedance-2.0.
- Khi dola trả lời «需要消耗 N 个，今日剩余 M 个，无法生成» (cần N, hôm nay còn M, không thể tạo), dola **không trừ điểm**: hệ thống chỉ điều chỉnh số dư của tài khoản này về M, tài khoản vẫn nhận được các đơn rẻ hơn, không bị khóa cả ngày.
- **30 giây**: từ 2026-10-12, với yêu cầu 30 giây dola ngoài miệng trả lời «chỉ hỗ trợ 4～15 giây, sẽ tạo 15 giây», nhưng thực tế vẫn tạo video 30 giây. Mặc định hệ thống từ chối thẳng yêu cầu 30 giây (báo lý do cho bên gọi, không chiếm hạn mức); quản trị viên có thể đặt `DOLA_30S_MODE=pair` trong `dola.env` (khuyến nghị): hệ thống **trước tiên gửi nguyên yêu cầu 30 giây**. dola sẽ nói «tạo 15 giây, trừ 2 điểm», nhưng thực tế đa số ra đủ 30 giây (ngày 10-14 đo 4 đơn đều 30.08 giây), **chỉ trừ 2 điểm**; nếu lỡ chỉ ra 15 giây, hệ thống sẽ bù thêm một lần nửa sau rồi ghép thành 30 giây (tổng 4 điểm). Khi dola hỏi lại «có muốn tạo 15 giây không», hệ thống sẽ tự trả lời «xác nhận tạo». Lưu ý: chỉ tài khoản nhập Cookie mới làm được 30 giây; video thành phẩm dưới 13 giây thì tác vụ thất bại luôn (không đổi tài khoản đốt thêm hạn mức); khi có bù nửa sau, hình ảnh giữa hai đoạn có thể không hoàn toàn liền mạch; dola thường phải xếp hàng hàng chục phút (hay báo «预计等待 1 小时», dự kiến chờ 1 giờ).
- Tạo thất bại thì **không trừ** điểm của tài khoản.
- Mỗi ngày lúc **0:00 giờ Tokyo (23:00 giờ Bắc Kinh)** hệ thống tự động đặt lại hạn mức của mọi tài khoản về đầy.

### 2.3 Nhóm (quan trọng nhất)

Hệ thống tự động xếp mỗi tài khoản vào một “nhóm” dựa trên trạng thái của nó. **Khi phân phát tác vụ chỉ dùng tài khoản thuộc các nhóm bình thường.**

| Nhóm | Ý nghĩa | Có được giao tác vụ không | Thoát khỏi nhóm thế nào |
|---|---|---|---|
| 🟢 **Đủ hạn mức (满额)** | Còn 4 điểm | ✅ `seedance-2.0` chỉ dùng nhóm này; 2.5 cũng dùng sau khi nhóm Nửa hạn mức đã hết | Tự thay đổi sau khi dùng hạn mức |
| 🔵 **Nửa hạn mức (半额)** | Còn 2～3 điểm | ✅ `seedance-2.5` ưu tiên dùng nhóm này | Như trên |
| 🟠 **Làm nguội (冷却)** | Còn 0～1 điểm (không đủ tạo một video) | ❌ | Sau lần đặt lại lúc 23:00 hằng ngày tự về Đủ hạn mức |
| 🔵 **Đang tạo (生成中)** | Đang tạo video | ❌ (đang bận) | Tạo xong tự quay về |
| 🔴 **Rủi ro (风控)** | Bị dola đăng xuất, hoặc gửi “xin chào” không có phản hồi | ❌ | **Bắt buộc bấm «Khôi phục (恢复)» thủ công**, rồi xác minh lại |
| ⚪ **Chờ kích hoạt (待激活)** | Tài khoản mới, chưa xác nhận dùng được | ❌ | Bấm «Xác thực (验证)» hoặc «Kiểm tra "xin chào" (你好探测)», đạt thì tự kích hoạt |
| 🔴 **Bất thường (异常)** | Giao tác vụ 5 phút mà không có phản hồi gì, hoặc thất bại liên tiếp 3 lần | ❌ | Tự khôi phục sau 30 phút, cũng có thể «Khôi phục» thủ công |

> **Hợp lệ = Đủ hạn mức + Nửa hạn mức + Làm nguội + Đang tạo**, tức là “các tài khoản không có vấn đề”.

### 2.4 Proxy

dola.com **chỉ cho phép truy cập từ IP Nhật Bản / Hàn Quốc**. Nếu máy chủ không đặt ở Nhật/Hàn, bắt buộc phải cấu hình proxy cho tài khoản, nếu không thì thêm tài khoản, xác minh, tạo video đều sẽ thất bại.

- **IP tĩnh**: một IP đầu ra cố định, điền địa chỉ + cổng + tên người dùng/mật khẩu.
- **IP động**: một nhóm node được dùng luân phiên, mỗi node gán cho một tài khoản (tránh nhiều tài khoản dùng chung một IP bị liên kết với nhau).

Tài khoản không được cấu hình proxy riêng sẽ đi qua proxy mặc định trong biến môi trường `DOLA_PROXY`.

#### Cách ly nghiêm ngặt tài khoản Cookie (tùy chọn)

Tài khoản Cookie mặc định đi qua giao thức HTTP thuần. Nếu cần buộc tài khoản Cookie Dola gắn cố định với một môi trường trình duyệt, hãy dùng kiểu triển khai Chromium/Xvfb không qua Docker,
cài CloakBrowser wrapper, rồi đặt trong `dola.env`:

```env
DOLA_COOKIE_RUNTIME=cloak
DOLA_CLOAK_HEADLESS=0
DOLA_ISOLATE_SHARED_EGRESS=1
DOLA_EGRESS_THROTTLE_ROTATE=0
DOLA_EGRESS_THROTTLE_MIN_ACCOUNTS=1
DOLA_MIN_SUBMIT_INTERVAL_SECONDS=15
CLOAKBROWSER_CACHE_DIR=/opt/dola-pool/.cloakbrowser
```

Ở chế độ này, mỗi tài khoản Cookie của dola.com dùng một profile lưu bền riêng `accounts/<账号>/cloak-profile/` (`<账号>` = tên tài khoản),
và gắn với proxy cùng sticky session của chính tài khoản đó. Khi trình duyệt không dùng được, hệ thống sẽ không âm thầm quay về API thuần; tài khoản Cookie Doubao
vẫn giữ đường API thuần của Doubao. Tài khoản đã có, ở lần chạy đầu tiên, sẽ được dựng lại Cookie và Web Storage có thể khôi phục từ `cookie_state.json`.
Không sao chép profile, `.cloakbrowser-seed`, và không dùng lại IP đầu ra giữa các tài khoản.

`710022002` là tín hiệu giới hạn tần suất loại “truy cập quá thường xuyên” từ upstream. Ở chế độ nghiêm ngặt, cùng một IP đầu ra không chạy đồng thời, khoảng cách giữa hai lần gửi của cùng một tài khoản mặc định là 15 giây;
khi bị dính sẽ lùi lại (backoff) riêng theo IP đầu ra và theo tài khoản; mặc định không tự đổi IP, để tránh cùng một phiên Cookie bị nhảy thiết bị/IP trong thời gian ngắn.
Trước khi điều chỉnh, hãy xác nhận proxy thực sự là IP đầu ra cố định hoặc sticky riêng cho từng tài khoản.

Thứ tự nhập ở chế độ nghiêm ngặt: khi nhập hàng loạt, đánh dấu «Bỏ qua xác minh trực tuyến (跳过在线验证)» → nhập xong thì gán proxy riêng cho từng tài khoản →
rồi bấm «Xác thực (验证)» lần lượt từng tài khoản. Như vậy lần đầu truy cập Dola sẽ dùng IP đầu ra của chính tài khoản, không đi qua proxy mặc định toàn cục trước.

Image Docker hiện tại theo thiết kế chỉ chứa các phụ thuộc cho API thuần, không chứa CloakBrowser/Chromium; với chế độ nghiêm ngặt hãy dùng
kiểu triển khai Xvfb/systemd của `deploy/install.sh`, hoặc tự build image có kèm phụ thuộc trình duyệt.

### 2.5 API Key

“Chìa khóa” mà bên gọi phải kèm theo khi gọi API (`Authorization: Bearer sk-xxx`). Có hai nguồn:

| Nguồn | Xem ở đâu | Giới hạn |
|---|---|---|
| Tạo trong mục «Khóa API (API 密钥)» trên bảng quản trị | Trong danh sách trên bảng quản trị | Có thể giới hạn riêng số tác vụ mỗi ngày, số đồng thời, thời lượng cho phép, thời điểm hết hạn |
| Nằm trong biến môi trường `DOLA_API_KEYS` | Không thấy trên bảng quản trị (chỉ hiện số lượng) | **Hoàn toàn không giới hạn**, chỉ dùng cho người nhà |

> Phát **Key khác nhau** cho từng khách hàng, như vậy sẽ thấy được ai gọi ở cột «Khách hàng (客户)» trong danh sách tác vụ, và có thể giới hạn riêng.

### 2.6 Tác vụ

Mỗi lần bên gọi gửi một yêu cầu tạo = một tác vụ. Tác vụ chỉ có bốn trạng thái:

```
Đang xếp hàng(queued) ──► Đang tạo(processing) ──► Hoàn thành(completed)  → có link video
                                             └──► Thất bại(failed)       → có lý do thất bại
```

- **Đang xếp hàng**: đang chờ tài khoản rảnh, **đây là hiện tượng bình thường**, đừng gửi lặp lại.
- **Đang tạo**: đã giao cho một tài khoản tạo video, thường mất **3～10 phút**, video 30 giây có thể lâu hơn.
- Khi thất bại sẽ **tự đổi tài khoản thử lại**: mặc định trong tổng thời hạn 600 giây (`DOLA_TASK_DEADLINE`) liên tục đổi tài khoản, hết thời gian vẫn chưa thành công mới tính là thất bại. Tác vụ đã có phiên dola và chỉ đang chờ video thì sẽ không đổi tài khoản gửi lại, tránh bị trừ hạn mức hai lần.

---

## 3. Bắt đầu lần đầu: từ con số 0 đến video đầu tiên

Giả sử dịch vụ đã được triển khai xong, có thể mở bảng quản trị qua `https://tên-miền-của-bạn`.

### Bước 1: Đăng nhập và đổi mật khẩu mặc định

1. Mở `https://tên-miền-của-bạn/` trên trình duyệt, khung đăng nhập hiện ra.
2. Tài khoản mặc định: **`admin` / `admin123`**.
3. Sau khi đăng nhập vào «**Quản lý người dùng (用户管理)**» → ở dòng admin bấm «Sửa (编辑)» → điền mật khẩu mới → lưu.

> ⚠️ Nhất định phải đổi! Mật khẩu mặc định được công khai trong code.

### Bước 2: Cấu hình proxy (bắt buộc khi máy chủ không ở Nhật Bản hoặc Hàn Quốc)

1. Vào «**Quản lý proxy (代理管理)**» → góc trên bên phải bấm «**＋ Thêm proxy (＋ 添加代理)**».
2. Điền tên (ví dụ “Proxy chính Nhật Bản”), chọn giao thức (HTTP / SOCKS5 …) và loại IP đầu ra:
   - **IP tĩnh**: điền địa chỉ, cổng, tên người dùng, mật khẩu.
   - **IP động**: chọn “Link trích xuất” hoặc “Dán node trực tiếp” (mỗi dòng một `host:port:用户名:密码`, tức `host:port:tên người dùng:mật khẩu`).
3. Lưu xong bấm «**Kiểm tra (探测)**» ở dòng đó, xác nhận IP đầu ra nằm ở Nhật Bản / Hàn Quốc.

### Bước 3: Thêm tài khoản

Vào «**Quản lý tài khoản (账号管理)**»:

- **Thêm một tài khoản**: bấm «**＋ Thêm tài khoản (＋ 添加账号)**»
  - Tab «Đăng nhập Google (Google 登录)»: điền email, mật khẩu, khóa TOTP → Bắt đầu đăng nhập (máy chủ sẽ tự mở trình duyệt để đăng nhập, mất vài chục giây).
  - Tab «Nhập Cookie (Cookie 导入)»: dán Cookie JSON xuất từ tiện ích trình duyệt (Cookie-Editor …) → Nhập tài khoản.
- **Thêm nhiều tài khoản một lúc**: bấm «**＋ Thêm hàng loạt (＋ 批量添加)**», có ba chế độ:
  - **Thêm hàng loạt bằng văn bản (文本批量添加)**: mỗi dòng một tài khoản, định dạng `账号名 邮箱 密码 TOTP` (tên tài khoản, email, mật khẩu, TOTP), cũng hỗ trợ `邮箱|密码`, `邮箱---密码` (email|mật khẩu; có thể bỏ tên tài khoản, hệ thống tự đặt là accN). Nhiều tài khoản sẽ **xếp hàng đăng nhập lần lượt**, hãy kiên nhẫn chờ.
  - **Tệp Cookie JSON (Cookie JSON 文件)**: chọn một lần nhiều tệp `.json`, mỗi tệp một tài khoản, tên tài khoản mặc định lấy theo tên tệp.
  - **Văn bản Cookie header (Cookie 头文本)**: mỗi dòng dán một header Cookie (`sessionid=...; ...`), tự loại trùng, tự đặt tên.
    **Cách xuất Cookie từ trình duyệt xem [`COOKIE.vi.md`](COOKIE.vi.md)** (trong hộp thoại của bảng quản trị cũng có phần hướng dẫn thu gọn «Lấy Cookie thế nào? (怎么获取 Cookie？)»).

> Khi nhập Cookie có tùy chọn «Bỏ qua xác minh trực tuyến (跳过在线验证)»: đánh dấu thì nhập vào trước, đợi cấu hình xong proxy rồi xác minh đồng loạt.

### Bước 4: Gán proxy cho tài khoản

- Từng tài khoản: ở dòng tài khoản bấm nút «Proxy (mặc định) (代理(默认))» → chọn proxy.
- Hàng loạt: đánh dấu nhiều tài khoản → trên cùng bấm «**Đổi proxy (更改代理)**» → chọn proxy.

### Bước 5: Kích hoạt tài khoản

Tài khoản mới mặc định nằm ở nhóm 【Chờ kích hoạt (待激活)】, sẽ không nhận tác vụ. Chọn một trong các cách kích hoạt:

- Đánh dấu tài khoản → «**Xác thực nhanh (一键验证)**»: kiểm tra trạng thái đăng nhập còn hiệu lực không (mỗi tài khoản khoảng 10～40 giây).
- «**Kiểm tra "xin chào" (你好探测)**»: gửi cho mỗi tài khoản một câu “xin chào” (你好), trả lời bình thường là đạt (không đánh dấu thì sẽ kiểm tra tất cả tài khoản đang được lọc ra).

Đạt xong tài khoản vào nhóm 【Đủ hạn mức (满额)】, có thể nhận tác vụ.

### Bước 6: Tự thử một video trước

Ở một dòng tài khoản bấm «**Thử tạo (测试生成)**» → chọn mô hình, thời lượng, tỷ lệ khung hình, điền prompt → bắt đầu.

> Việc này sẽ **tiêu hao thật hạn mức của tài khoản đó**, dùng để xác nhận tài khoản thực sự tạo được video.

### Bước 7: Phát API Key cho bên gọi

1. Vào «**Khóa API (API 密钥)**» → «**＋ Tạo khóa (＋ 创建密钥)**».
2. Điền tên (ví dụ “Khách hàng A”), thiết lập tùy nhu cầu:
   - Giới hạn tác vụ mỗi ngày (0 = không giới hạn)
   - Giới hạn số video tạo cùng lúc (0 = không giới hạn)
   - Thời lượng video cho phép (đánh dấu chọn)
   - Thời điểm hết hạn (để trống = vĩnh viễn)
3. Tạo xong sao chép Key, gửi cho bên gọi cùng với `https://tên-miền-của-bạn`, kèm theo [`API.md`](API.md).

Tới đây bên gọi đã có thể bắt đầu tạo video, mọi tác vụ đều xem được trong «Danh sách tác vụ (任务列表)».

---

## 4. Giải thích từng trang của bảng quản trị

Bên trái bảng quản trị là thanh điều hướng, góc dưới bên trái có vài nút toàn cục:

| Nút | Tác dụng |
|---|---|
| Tự động làm mới (自动刷新) | Đánh dấu thì cứ 10 giây tự làm mới dữ liệu trang hiện tại |
| Làm mới (刷新) | Làm mới thủ công một lần |
| Kiểm tra tải (压力测试) | Đo xem máy chủ mở tối đa được bao nhiêu trình duyệt cùng lúc (xem [4.8](#48-kiểm-tra-tải)) |
| Đăng xuất (注销) | Thoát đăng nhập |

### 4.1 Bảng điều khiển

Nhìn tổng quan trong một lần:

- **Video hôm nay (今日出片) / Thất bại hôm nay (今日失败) / Tỷ lệ thành công (成功率)**
- **Tài khoản khả dụng X/Y · còn N điểm (可用号 X/Y · 剩余 N 点)**: Y là tổng số tài khoản, X là số tài khoản nhận được tác vụ, N là tổng số điểm còn lại của các nhóm bình thường.
- **Xu hướng tạo video 7 ngày qua (近 7 日出片趋势)**: màu xanh lá là thành công, màu đỏ là thất bại.
- **Tổng quan pool tài khoản (号池概况)**: số lượng của từng nhóm, bấm vào nhóm nào sẽ nhảy thẳng sang Quản lý tài khoản và lọc theo nhóm đó; dòng “hạn mức còn lại ≈ 2.5 tạo được N video” bên dưới là ước tính theo seedance-2.5 xem hôm nay còn tạo được bao nhiêu video.

### 4.2 Quản lý tài khoản

#### Thanh công cụ phía trên

| Điều khiển | Tác dụng |
|---|---|
| Tất cả nguồn (全部来源) ▾ | Lọc theo cách thêm: Đăng nhập tài khoản (账号登录) / Nạp Cookie (Cookie 载入) |
| Dải nút nhóm | Tất cả (全部) / Hợp lệ (有效) / Đủ hạn mức (满额) / Nửa hạn mức (半额) / Làm nguội (冷却) / Đang tạo (生成中) / Rủi ro (风控) / Chờ kích hoạt (待激活) / Bất thường (异常), bấm để lọc, con số là số lượng |
| Xóa mục đã chọn (删除选中) | Xóa các tài khoản đã đánh dấu (kèm dữ liệu trình duyệt của nó, **không thể khôi phục**) |
| Xác thực nhanh (一键验证) | Lần lượt kiểm tra trạng thái đăng nhập của các tài khoản đã đánh dấu |
| Kiểm tra "xin chào" (你好探测) | Gửi “xin chào” để xem có bị Rủi ro không; **không đánh dấu thì kiểm tra toàn bộ kết quả đang lọc** |
| Khôi phục hàng loạt (批量恢复) | Đưa các tài khoản 【Rủi ro】/【Bất thường】 đã đánh dấu trở lại quy trình (tài khoản Rủi ro sau khi khôi phục về 【Chờ kích hoạt】, nhớ kiểm tra lại một lần) |
| Đổi proxy (更改代理) | Đổi proxy hàng loạt cho các tài khoản đã đánh dấu |
| Đặt lại hạn mức (重置额度) | Ngay lập tức khôi phục đầy hạn mức cho cả pool (bình thường mỗi ngày 23:00 tự chạy, thường không cần bấm tay) |
| ＋ Thêm hàng loạt (＋ 批量添加) / ＋ Thêm tài khoản (＋ 添加账号) | Xem [Bước 3](#bước-3-thêm-tài-khoản) |

> **Mẹo đánh dấu**: ô chọn tất cả ở đầu bảng = chọn **toàn bộ tài khoản trong kết quả lọc hiện tại** (giữ nguyên khi chuyển trang). Ví dụ muốn dọn sạch tài khoản Rủi ro: bấm nhóm 【Rủi ro】 trước → chọn tất cả → Xóa mục đã chọn. Khi đổi bộ lọc, các tài khoản bị lọc ra ngoài sẽ tự bỏ đánh dấu, không xóa nhầm tài khoản bạn không nhìn thấy.

#### Các cột trong bảng

| Cột | Giải thích |
|---|---|
| Tài khoản nội bộ (内部账号) | Tên trong hệ thống (acc1, acc2…), có thể bấm «Đổi tên (改名)» |
| Tên (名称) | Email của tài khoản, chưa nhập thì hiện “Chưa nhập email (未录入邮箱)” |
| Nhóm (组) | Nhóm hiện tại, dòng chữ nhỏ bên dưới là lý do |
| Hoạt động (运行) | Đang tạo video (出片中) / Đăng nhập hết hiệu lực (登录失效) / Đang thêm tài khoản (加号中) / Thêm tài khoản thất bại (加号失败); tài khoản dùng proxy động còn hiện IP đầu ra và «Đổi IP (换IP)», «Kiểm tra (探测)» |
| Nguồn (来源) | Đăng nhập tài khoản / Tải Cookie |
| Điều phối (调度) | Công tắc. **Tắt đi thì tài khoản này không nhận tác vụ nữa** (tạm ngưng một tài khoản thì dùng cái này, không cần xóa) |
| Hạn mức còn lại (剩余额度) | Hôm nay còn bao nhiêu điểm / giới hạn mỗi ngày |
| Dùng gần nhất (最近使用) / Thời gian tạo (创建时间) | — |
| Ghi chú (备注) | Bấm để sửa; nguyên văn lý do dola từ chối (như bảo vệ chân dung) cũng được tự ghi lại ở đây |

#### Các nút thao tác trên mỗi dòng

| Nút | Tác dụng |
|---|---|
| Đổi tên (改名) | Sửa tên tài khoản nội bộ |
| Xác thực (验证) | Kiểm tra trạng thái đăng nhập của tài khoản này |
| Thử tạo (测试生成) | Dùng tài khoản này tạo thật một video (trừ hạn mức). Nếu tài khoản đang tạm dừng do Rủi ro/giới hạn tần suất, IP đầu ra đang bị giới hạn tần suất, chưa gán proxy hoặc hạn mức hôm nay không đủ, sẽ hiện hộp thoại liệt kê lý do trước, xác nhận rồi mới gửi |
| Nút proxy | Hiện tên proxy đang gán, bấm để đổi |
| Kiểm tra "xin chào" (你好探测) | (Chỉ tài khoản Rủi ro/Chờ kích hoạt mới có) kiểm tra thủ công một lần |
| Khôi phục (恢复) | (Chỉ tài khoản Rủi ro/Bất thường mới có) đưa trở lại quy trình |
| Xóa (删除) | Xóa tài khoản |

### 4.3 Quản lý proxy

Liệt kê tất cả proxy: tên, loại (tĩnh/động), giao thức, địa chỉ, số tài khoản đã gán, IP đầu ra ….

| Thao tác | Tác dụng |
|---|---|
| ＋ Thêm proxy (＋ 添加代理) | Tạo proxy mới, xem [Bước 2](#bước-2-cấu-hình-proxy-bắt-buộc-khi-máy-chủ-không-ở-nhật-bản-hoặc-hàn-quốc) |
| Thêm nhanh (快捷添加) | Tab thứ hai trong hộp thoại «Thêm proxy»: mỗi dòng dán một proxy để thêm hàng loạt, có thể tự kiểm tra IP đầu ra có ở Nhật Bản/Hàn Quốc không |
| Sửa (编辑) | Sửa proxy; proxy động có thể thêm node |
| Kiểm tra (探测) | Kiểm tra IP đầu ra hiện tại của proxy này và nơi đăng ký IP |
| Pool node (节点池) | (Proxy động) xem node, tình trạng sống và IP đầu ra, có thể bổ sung hoặc dọn node |
| Đổi IP (换 IP) | Đổi IP đầu ra cho tất cả tài khoản đang gán với proxy này |
| Xóa (删除) | Sau khi xóa, các tài khoản gán với nó tự chuyển về proxy mặc định |

Tùy chọn nâng cao của proxy động:
- **Khoảng giới hạn tần suất trích xuất (提取限流间隔)**: khoảng cách tối thiểu giữa hai lần gọi link trích xuất, tránh tiêu hết hạn mức trích xuất.
- **Khoảng đổi IP tối thiểu (最小换 IP 间隔)**: khoảng cách tối thiểu giữa hai lần đổi IP của cùng một tài khoản.
- **Tự động đổi IP đầu ra khi bị kiểm soát rủi ro (被风控时自动换出口 IP)**: khuyến nghị luôn đánh dấu.

### 4.4 Danh sách tác vụ

Hiển thị **50 tác vụ gần nhất**, các cột:

| Cột | Giải thích |
|---|---|
| ID | Mã tác vụ (rê chuột để xem ID đầy đủ) |
| Prompt (提示词) | Bấm «Xem (查看)» để xem prompt đầy đủ và có thể sao chép |
| Ảnh tham chiếu (参考图) | Ảnh thu nhỏ của ảnh tham chiếu khi tạo video từ ảnh, bấm để phóng to |
| Tài khoản (账号) | Đã dùng tài khoản nào; nếu hiện `acc1 → acc10 → acc11` nghĩa là giữa chừng đã đổi tài khoản |
| Khách hàng (客户) | Gửi bằng API Key nào (tên của Key) |
| Thời lượng (时长) | Thời lượng video |
| Trạng thái (状态) | Đang xếp hàng / Đang tạo / Hoàn thành / Thất bại |
| **Thời gian xử lý (耗时)** | Hoàn thành/thất bại: **thời gian tạo**; nếu xếp hàng lâu thì bên dưới có thêm một dòng tổng thời gian “gồm xếp hàng X” (含排队 X); rê chuột để xem chi tiết «xếp hàng · tạo · tổng». Đang tạo thì hiện “đã X” (已 X), đang xếp hàng thì hiện “xếp hàng X” (排队 X) |
| Thời gian tạo (创建时间) | Thời điểm gửi |
| Video (视频) | «Mở (打开)» phát trong cửa sổ mới, «Xem trước (预览)» phát ngay trong bảng quản trị |
| Lỗi/Ghi chú (报错/备注) | Nguyên văn lý do thất bại, xem [8.4 Tác vụ thất bại, xem nguyên nhân thế nào](#84-tác-vụ-thất-bại-xem-nguyên-nhân-thế-nào); trong quá trình tạo còn hiện `dola：…`, tức nguyên văn câu dola trả lời (như «预计等待 45 分钟», dự kiến chờ 45 phút) |

### 4.5 Quản lý video

Toàn bộ video đã tạo thành công: phía trên hiển thị tổng số video, bảng có các cột thời gian tạo, thời lượng, kích thước, dung lượng, tài khoản, mô hình, trạng thái, prompt; có thể xem trước, sao chép link, xóa từng cái hoặc xóa hàng loạt.

> ⚠️ Xóa video sẽ **xóa đồng thời tệp video và bản ghi tác vụ, không thể khôi phục**, `video_url` mà bên gọi đang giữ cũng sẽ mất hiệu lực.

### 4.6 Khóa API

| Cột | Giải thích |
|---|---|
| Key | Bản thân khóa, bấm «Sao chép (复制)» là sao chép được |
| Tên (名称) | Tên khách hàng |
| Thời lượng cho phép (允许时长) | Key này tạo được những thời lượng nào |
| Tác vụ hôm nay (今日任务) | Hôm nay đã gửi / giới hạn mỗi ngày |
| Đồng thời (并发) | Đang chạy cùng lúc / giới hạn đồng thời |
| Bật (启用) | Công tắc, tắt đi thì Key này lập tức không gọi được nữa |
| Dùng gần nhất (最近使用) | — |
| Thao tác (操作) | Sửa (编辑) / Xóa (删除) |

> Giới hạn tác vụ mỗi ngày tính theo **số lần gửi**, bất kể thành công hay thất bại.

### 4.7 Quản lý người dùng

Quản lý những người được đăng nhập bảng quản trị:

- **＋ Thêm quản trị viên (＋ 新增管理员)**: mở tài khoản phụ cho đồng nghiệp.
- **Sửa (编辑)**: đổi mật khẩu, bật/tắt.
- Tài khoản chính `admin` không thể xóa, vô hiệu hóa hay đổi tên, nhưng có thể đổi mật khẩu.

### 4.8 Kiểm tra tải

Dùng để đo **máy chủ này mở tối đa được bao nhiêu trình duyệt cùng lúc** (đường tạo video bằng trình duyệt sẽ dùng tới). Máy chủ sẽ bắt đầu từ 1 rồi tăng dần, mở đồng thời 1, 2, 3…N trình duyệt, thống kê bộ nhớ, cuối cùng đưa ra “số đồng thời khuyến nghị”.

- Không truy cập dola, **không tiêu hao bất kỳ hạn mức nào**.
- Sẽ bị từ chối khi có tác vụ đang chạy hoặc đang thêm tài khoản, hãy chạy vào lúc rảnh.

---

## 5. Bên gọi API sử dụng thế nào (bản tối giản)

Hướng dẫn đầy đủ nằm trong [`API.md`](API.md), ở đây chỉ đưa quy trình ngắn nhất:

```bash
BASE=https://tên-miền-của-bạn
KEY=sk-xxxxxxxx      # tạo trong mục «Khóa API (API 密钥)» trên bảng quản trị

# ① Gửi tác vụ (trả về ID tác vụ ngay, không đợi video tạo xong)
curl -X POST "$BASE/v1/videos/generations" \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model":"seedance-2.5","prompt":"一只橘猫在草地上追蝴蝶","duration":10,"size":"1280x720"}'
# → {"id":"video_xxx","status":"queued",...}

# ② Cứ 5 giây hỏi một lần, đến khi status thành completed hoặc failed
curl "$BASE/v1/videos/video_xxx" -H "Authorization: Bearer $KEY"
# → khi completed sẽ kèm "video_url": "https://tên-miền-của-bạn/videos/xxx.mp4"

# ③ Tải video
curl -o out.mp4 "$BASE/v1/videos/video_xxx/content" -H "Authorization: Bearer $KEY"
```

(Prompt trong ví dụ `一只橘猫在草地上追蝴蝶` nghĩa là “một chú mèo cam đuổi bướm trên bãi cỏ”.)

Ba lời nhắc dành cho bên gọi:

1. **Việc gửi là bất đồng bộ**, bắt buộc phải hỏi vòng (polling) để lấy kết quả; tạo video thường mất 3～10 phút, video 30 giây lâu hơn, hãy đặt timeout phía client tới 30 phút.
2. **Thấy `queued` thì đừng gửi lại**, đó chỉ là đang xếp hàng, gửi lặp sẽ chiếm hạn mức lặp.
3. Kích thước: `1280x720` ngang, `720x1280` dọc, `1024x1024` vuông; ảnh tham chiếu dùng `reference_images: ["图片URL"]` (`图片URL` = URL ảnh).

Muốn tích hợp với **new-api**, xem phần «Kết nối với new-api» trong [`README.vi.md`](README.vi.md).

---

## 6. Vận hành hằng ngày

### Mỗi ngày xem qua một lần

1. **Bảng điều khiển (仪表盘)**: tỷ lệ thành công có bình thường không, số điểm còn lại có đủ dùng hôm nay không.
2. **Quản lý tài khoản (账号管理) → nhóm Rủi ro (风控) / Bất thường (异常)**: số lượng đột ngột tăng nhiều nghĩa là proxy hoặc tài khoản đang có vấn đề.
3. **Danh sách tác vụ (任务列表)**: có nhiều tác vụ thất bại cùng một lý do không.

### Không đủ tài khoản

- Số điểm còn lại thường xuyên thấp → thêm tài khoản (Thêm hàng loạt (批量添加)).
- 23:00 giờ Bắc Kinh hạn mức sẽ tự đặt lại, nếu cần gấp có thể bấm «Đặt lại hạn mức (重置额度)» (chỉ khôi phục các tài khoản bị chặn vì hạn mức, tài khoản Rủi ro sẽ không được khôi phục).

### Tài khoản bị Rủi ro

1. Bấm nhóm 【Rủi ro (风控)】, xem lý do ở dòng chữ nhỏ bên dưới nhóm.
2. Nếu là **vấn đề proxy** (ví dụ IP đầu ra không ở Nhật/Hàn): sửa proxy trước → đánh dấu các tài khoản này → «Khôi phục hàng loạt (批量恢复)» → «Kiểm tra "xin chào" (你好探测)».
3. Nếu là **tài khoản bị đăng xuất**: tài khoản Cookie cần nhập lại Cookie; tài khoản đăng nhập Google có thể xóa rồi thêm lại.
4. Tài khoản thực sự không cứu được: chọn → «Xóa mục đã chọn (删除选中)».

### Giới hạn / ngưng một khách hàng

Trong «Khóa API (API 密钥)» sửa Key của khách hàng đó, đổi giới hạn mỗi ngày, giới hạn đồng thời, hoặc tắt hẳn công tắc «Bật (启用)».

---

## 7. Về việc xóa watermark

### 7.1 Kết luận: không phải “xóa” watermark, mà là lấy thẳng phiên bản không watermark của dola

Mỗi video dola tạo ra, trên máy chủ dola vốn đã có **hai phiên bản**:

| Phiên bản | Đặc điểm link | Giải thích |
|---|---|---|
| Có watermark | `lr=cici_ai` | Địa chỉ tải mặc định trên trang web |
| Không watermark | `lr=unwatermarked` hoặc `logo_type=unwatermarked` | Một link có chữ ký khác của cùng video |

Dự án này **không xử lý gì trên hình ảnh** (không làm mờ, không cắt, không vá vùng watermark), chỉ tìm cách lấy link thứ hai, rồi tải về lưu lại trên máy chủ này. Vì vậy chất lượng video thành phẩm giống hệt video gốc của dola.

### 7.2 Ba cách lấy

Các loại tài khoản khác nhau đi theo đường tạo video khác nhau, cách lấy link không watermark cũng khác:

**① Tìm thẳng trong dữ liệu dola trả về (không phụ thuộc bên thứ ba nào)**

- **Đường trình duyệt** (tài khoản thêm bằng đăng nhập Google): sau khi tạo video, trước tiên gọi API phát video của chính dola (`fallback_api`), đổi tham số thành `logo_type=unwatermarked`, `codec_type=8`, lấy bản H.264 bitrate cao không watermark (khoảng 3 Mbps). Nếu bước này thất bại, sẽ đọc trường `video_model.video_list.*.main_url` trong dữ liệu trả về (mã hóa base64, giải mã ra chính là link `lr=unwatermarked`), lấy cái có bitrate cao nhất. Code nằm ở `fetch_higher_bitrate_url()` trong `video_worker_ui.py` và `extract_unwatermarked_url()` trong `video_worker.py`.
- **Đường API thuần** (tài khoản nhập Cookie): tìm trong thông tin phát video dola trả về link có tham số `unwatermarked`. Code nằm ở `find_direct_nowatermark_urls()` trong `protocol/dola_pure_api.py`.

**② API giải mã xóa watermark của bên thứ ba (đường API thuần, mặc định tắt)**

- Công tắc là `DOLA_PURE_REMOVE_WATERMARK`, **mặc định `0` (tắt)**. Trước 2.1.5 mặc định là `1` (bật).
- Chỉ khi đặt thành `1`, đường API thuần mới gửi địa chỉ phát video (`fplay_url`) hoặc thông tin video (`video_info`) tới API bên thứ ba `http://47.104.150.143:8765/tools/dola/`, để bên đó trả về địa chỉ không watermark. API này đến từ plugin remove_water của bên thứ ba, địa chỉ được ghi trong `NOWATERMARK_API_ENDPOINT` ở `protocol/dola_pure_api.py`.
- Khi API bên thứ ba thất bại, tự quay về link trực tiếp của cách ①; nếu cũng không có link trực tiếp, chỉ còn cách dùng link gốc có watermark.

**③ Tiện ích trình duyệt `extensions/dola30`**

- Tiện ích Chrome của bên thứ ba “豆包 Dola 15秒去水印助手” (trợ lý xóa watermark 15 giây Doubao Dola), được nạp trong đường trình duyệt (`DOLA_EXTENSION_ENABLED=1`).
- Nguyên lý giống ①: khi gọi API phát video của dola thì kèm `logo_type=unwatermarked`, xin dola bản không watermark. Nó còn đảm nhiệm các chức năng như thời lượng 30 giây.

### 7.3 ⚠️ Rủi ro về quyền riêng tư và phụ thuộc

**Khi bật API bên thứ ba (`DOLA_PURE_REMOVE_WATERMARK=1`)**, với mỗi video do tài khoản Cookie tạo ra, thông tin phát video đều được gửi qua HTTP không mã hóa tới máy chủ bên thứ ba `47.104.150.143`, điều đó có nghĩa là:

- Bên đó lấy được địa chỉ của tất cả video của bạn, tức là có thể xem, tải các video này;
- Khi máy chủ bên thứ ba này sập hoặc ngừng hoạt động, hệ thống sẽ quay về link trực tiếp, nhưng nếu cũng không tìm thấy link trực tiếp, video thành phẩm sẽ có watermark.

> Đo thực tế 2026-10: API bên thứ ba này đã lâu không kết nối được, mỗi video đều phải chờ uổng khoảng 2 phút cho tới khi timeout, trong khi link trực tiếp không watermark của chính dola luôn dùng được (hai máy chủ chạy thật, tổng cộng 10 lần đều lấy được video thành phẩm không watermark). Vì vậy từ 2.1.5 trở đi mặc định tắt.

### 7.4 Xác nhận / tắt API bên thứ ba

Từ 2.1.5 trở đi, không đặt gì tức là tắt. Nếu trong `dola.env` của bạn có ghi rõ `DOLA_PURE_REMOVE_WATERMARK=1` (mẫu của phiên bản cũ sẽ ghi), hãy đổi thành:

```bash
DOLA_PURE_REMOVE_WATERMARK=0
```

Sau đó khởi động lại dịch vụ: `sudo systemctl restart dola-pool`.

Sau khi tắt **vẫn dùng link trực tiếp không watermark của dola theo cách ①**, chỉ là không gửi thông tin video cho bên thứ ba nữa.

---

## 8. Câu hỏi thường gặp

### 8.1 Bảng quản trị đã cập nhật / không thấy tính năng mới?

Trình duyệt đã lưu cache trang cũ. Nhấn **Ctrl + F5** (Mac: **Cmd + Shift + R**) để buộc làm mới.

### 8.2 Đột nhiên hiện khung đăng nhập / báo “请先登录” (vui lòng đăng nhập trước), “登录已过期” (phiên đăng nhập đã hết hạn)?

Sau khi dịch vụ khởi động lại thì mọi người đều phải đăng nhập lại (trạng thái đăng nhập lưu trong bộ nhớ). Đăng nhập lại là được; nếu cứ hiện mãi, nhấn Ctrl + F5 làm mới cưỡng bức rồi đăng nhập. Phiên đăng nhập có hiệu lực 7 ngày.

### 8.3 Tác vụ cứ “Đang xếp hàng” mãi?

Nghĩa là tạm thời không có tài khoản khả dụng nào rảnh, các nguyên nhân thường gặp:

- Tài khoản trong các nhóm bình thường đều đang “Đang tạo (生成中)” → đợi một lát là xong.
- **seedance-2.0 chỉ dùng được tài khoản nhóm 【Đủ hạn mức (满额)】**, tài khoản Đủ hạn mức dùng hết thì sẽ phải xếp hàng (chờ tối đa 10 phút), có thể đổi sang seedance-2.5, hoặc đợi lần đặt lại lúc 23:00.
- Quá ít tài khoản hợp lệ → vào Quản lý tài khoản (账号管理) xem có phải nhiều tài khoản rơi vào Rủi ro (风控) / Chờ kích hoạt (待激活) không.

Xếp hàng quá 30 phút mà vẫn không có tài khoản, tác vụ sẽ tự bị tính là thất bại và ghi rõ lý do.

### 8.4 Tác vụ thất bại, xem nguyên nhân thế nào?

Xem cột cuối cùng «Lỗi/Ghi chú (报错/备注)» trong danh sách tác vụ. Các loại thường gặp:

| Trong thông báo lỗi có | Ý nghĩa | Làm gì |
|---|---|---|
| `肖像保护`, `未认证人脸` (bảo vệ chân dung, khuôn mặt chưa xác thực) | Ảnh tham chiếu có khuôn mặt người thật, dola từ chối tạo | Đổi ảnh tham chiếu hoặc chuyển sang tạo chỉ bằng văn bản |
| `内容审核`, `不合规`, `侵权` (kiểm duyệt nội dung, không hợp quy, vi phạm bản quyền) | Prompt hoặc hình ảnh kích hoạt kiểm duyệt của dola | Sửa prompt / hình ảnh |
| `generation_voided` / `视频生成失败，生成额度未扣除` (tạo video thất bại, chưa trừ hạn mức) | dola tạo thất bại ở phía họ (không trừ hạn mức) | Thường chỉ cần thử lại |
| `号池无可用账号` (pool tài khoản không có tài khoản khả dụng) | Tất cả tài khoản đều đang Làm nguội/Rủi ro/tắt điều phối | Thêm tài khoản, khôi phục tài khoản Rủi ro, hoặc đợi lần đặt lại lúc 23:00 |
| `出口 IP 都被上游限流` / `710022002 当前服务访问频繁` (IP đầu ra đều bị upstream giới hạn tần suất / dịch vụ đang bị truy cập quá thường xuyên) | dola giới hạn tần suất theo IP đầu ra | Xem [8.10](#810-kiểm-tra-hoặc-tạo-video-báo-710022002-当前服务访问频繁) |
| `等待出片超时：已等 N 分钟仍未出片（dola 预计排队 …）` (chờ tạo video quá thời gian: đã chờ N phút vẫn chưa có video, dola dự kiến xếp hàng …) | Phía dola xếp hàng quá lâu, vượt giới hạn chờ của dịch vụ này (mặc định tối đa 60 phút) | Hệ thống sẽ tiếp tục chờ video này ở chế độ nền (tối đa 3 giờ), có video là tác vụ tự chuyển thành «Hoàn thành», cột ghi chú hiện «✓ 超时后已自动取回» (đã tự lấy về sau khi quá thời gian), có thể gửi link cho khách hàng |
| `生视频过程中出现异常情况，请重试` (có tình huống bất thường trong quá trình tạo video, vui lòng thử lại) | Tài khoản được giao đi 5 phút mà không có phản hồi gì | Thử lại; nếu xảy ra thường xuyên thì proxy không ổn định |
| `连续 N 次生成均失败（依次尝试账号: …）` (N lần tạo liên tiếp đều thất bại, lần lượt thử các tài khoản: …) | Đã đổi N tài khoản đều thất bại, phần sau dấu hai chấm là lý do cụ thể của lần cuối | Xử lý theo lý do cụ thể sau dấu hai chấm |

### 8.5 Bên gọi nhận lỗi 429?

| Thông báo | Nguyên nhân |
|---|---|
| `所有已开启调度的账号均已达到 Dola 每日视频上限` (mọi tài khoản đang bật điều phối đều đã chạm giới hạn video mỗi ngày của Dola) | Hôm nay cả pool đã dùng hết hạn mức, đợi 23:00 hoặc thêm tài khoản |
| `API Key 今日额度已用完` (API Key đã dùng hết hạn mức hôm nay) | Key này đã chạm giới hạn tác vụ mỗi ngày |
| `待处理任务已达到服务上限` (số tác vụ chờ xử lý đã đạt giới hạn của dịch vụ) | Có quá nhiều tác vụ đang xếp hàng, thử lại sau |

### 8.6 Thêm tài khoản / xác minh cứ thất bại?

1. Trước tiên xác nhận IP đầu ra của proxy gán cho tài khoản ở **Nhật Bản / Hàn Quốc** (Quản lý proxy (代理管理) → Kiểm tra (探测)).
2. Tài khoản đăng nhập Google: kiểm tra email, mật khẩu, khóa TOTP có đúng không.
4. Báo `Browser does not support socks5 proxy authentication`: bản thân trình duyệt (Chromium) không hỗ trợ proxy socks5 có tên người dùng/mật khẩu. Từ 2.1.7 hệ thống sẽ tự dựng một lớp chuyển tiếp trên máy, chỉ cần nâng cấp; với phiên bản cũ hãy đổi proxy của tài khoản đăng nhập Google sang HTTP.
3. Xác minh tài khoản Cookie cần máy chủ có cài **Node.js**, khi xác minh báo “服务器异常” (máy chủ bất thường) hãy nhờ người vận hành kiểm tra.

### 8.7 Link video có hết hạn không?

Không. Video sau khi tạo sẽ được lưu cục bộ trên máy chủ, `video_url` là địa chỉ của dịch vụ này. Trừ khi bị xóa trong «Quản lý video (视频管理)», hoặc quá thời hạn lưu giữ và bị tự động dọn (mặc định 14 ngày, xem `DOLA_TASK_RETENTION_DAYS`).

### 8.8 Video tạo ra có watermark không?

Mặc định là không. Nguyên lý và rủi ro quyền riêng tư xem [7. Về việc xóa watermark](#7-về-việc-xóa-watermark).

### 8.9 Thất bại có bị trừ tiền không?

- **Hạn mức tài khoản**: chỉ tạo video thành công mới trừ, thất bại không trừ.
- **Giới hạn mỗi ngày của API Key**: tính theo số lần gửi, thất bại cũng tính một lần. Vì vậy bên gọi đừng dùng cùng một Key để thử sai liên tục.

### 8.10 Kiểm tra hoặc tạo video báo `710022002 当前服务访问频繁`?

(Nghĩa là “dịch vụ hiện đang bị truy cập quá thường xuyên”.) Đây là dola **giới hạn tần suất theo IP đầu ra**: cùng một IP gửi quá nhiều, quá dày thì sẽ bị từ chối. Bản thân tài khoản không có vấn đề, nên hệ thống **không** đánh nó thành Rủi ro, chỉ ghi “kiểm tra chưa hoàn tất”.

Hệ thống sẽ tự xử lý:
- Hễ một IP đầu ra bị từ chối, mọi tài khoản trên IP đầu ra đó sẽ **tạm dừng một thời gian** (bắt đầu từ 2 phút, bị từ chối liên tiếp thì nhân đôi, tối đa 15 phút), trong thời gian đó tác vụ đứng yên xếp hàng. Đầu trang Quản lý tài khoản (账号管理) sẽ hiện IP đầu ra nào đang bị giới hạn tần suất, còn bao lâu nữa thì khôi phục.
- Tài khoản bị từ chối sẽ vào nhóm 【Bất thường (异常)】 để tạm dừng, không tham gia phân phát (trong ghi chú ghi «上游限流 710022002（连续第 N 次）：暂停 X», tức “upstream giới hạn tần suất 710022002 (lần thứ N liên tiếp): tạm dừng X”), hết hạn tự khôi phục; kiểm tra thủ công đạt thì có thể khôi phục sớm. Nó **không** bị đánh thành Rủi ro. Thời gian tạm dừng nhân đôi theo số lần bị giới hạn tần suất liên tiếp: **30 phút → 1 giờ → 2 giờ → 4 giờ → 8 giờ** (mức trần); chỉ cần kiểm tra đạt hoặc tạo video thành công một lần là tính lại từ 30 phút.
- Nếu tất cả tài khoản đều đang bị tạm dừng, tác vụ sẽ thất bại ngay và báo «N 个号因上游限流暂停中，最早约 M 分钟后恢复» (N tài khoản đang tạm dừng do upstream giới hạn tần suất, sớm nhất khoảng M phút nữa khôi phục), không quay vòng chờ mãi.

Việc bạn cần làm:
1. **Đừng bấm «Kiểm tra "xin chào" (你好探测)» liên tục**, mỗi lần bấm lại đụng giới hạn tần suất thêm một lần, khiến thời gian tạm dừng dài hơn.
2. **Gán cho các tài khoản proxy có IP khác nhau**, khuyến nghị mỗi IP không quá 3 tài khoản (trang Quản lý tài khoản sẽ nhắc bằng màu vàng với IP đầu ra vượt mức). Lưu ý: hai cấu hình proxy nếu cùng địa chỉ và cổng thì thực chất là cùng một IP.
3. Khi máy chủ không ở Nhật Bản / Hàn Quốc, tài khoản **bắt buộc** phải gán proxy, kết nối trực tiếp chắc chắn không được.
4. Gán proxy xong, đợi thông báo biến mất, kiểm tra trước 1～2 tài khoản để xác nhận đạt, rồi mới xử lý theo từng đợt các tài khoản còn lại.

Nếu đã gán nhiều IP Nhật Bản khác nhau, đợi vài giờ mà mọi tài khoản vẫn luôn báo 710022002, thì đó không phải giới hạn tần suất, mà là dola đã cập nhật API, cần cập nhật code.

---

## 9. Bảng tra nhanh

### Trạng thái tài khoản → nên làm gì

| Thấy | Làm gì |
|---|---|
| Chờ kích hoạt (待激活) | Xác thực (验证) hoặc Kiểm tra "xin chào" (你好探测) |
| Rủi ro (风控) | Xem lý do → sửa proxy/nhập lại → Khôi phục (恢复) → Kiểm tra "xin chào" |
| Bất thường (异常) | Đợi 30 phút tự khôi phục, hoặc khôi phục thủ công |
| Làm nguội (冷却) | Không cần làm gì, 23:00 tự về Đủ hạn mức |
| Đăng nhập hết hiệu lực (登录失效) | Nhập lại Cookie hoặc đăng nhập lại |
| Công tắc Điều phối (调度) đang tắt | Tài khoản này đã bị người khác tạm ngưng thủ công, khi cần dùng thì bật lên |

### Các mốc thời gian quan trọng

| Sự kiện | Thời gian |
|---|---|
| Đặt lại hạn mức mỗi ngày | 0:00 Tokyo = **23:00 Bắc Kinh** |
| Nhóm Bất thường tự khôi phục | Sau 30 phút |
| Xếp hàng quá thời gian bị tính thất bại | 30 phút |
| Bảng quản trị tự làm mới | Mỗi 10 giây |
| Thời hạn phiên đăng nhập bảng quản trị | 7 ngày (khởi động lại dịch vụ là mất hiệu lực) |
| Thời hạn Cookie | Khoảng 60 ngày |

### Bản đồ tài liệu

| Tôi muốn… | Xem ở đâu |
|---|---|
| Dùng bảng quản trị để quản lý pool tài khoản | Tài liệu này |
| Viết code gọi API | [`API.md`](API.md) |
| Triển khai / chuyển máy chủ | [`README.vi.md`](README.vi.md), [`deploy/README.md`](deploy/README.md) |
| Xem mỗi phiên bản đã thay đổi gì | [`CHANGELOG.md`](CHANGELOG.md) |
| Sửa code | [`DEVELOPMENT.md`](DEVELOPMENT.md) |
