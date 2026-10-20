> 🌐 [中文](COOKIE.md) | **Tiếng Việt**

# Cách lấy Cookie của tài khoản dola

Tài khoản được nhập vào pool tài khoản bằng Cookie sẽ tạo video theo kiểu "API thuần": không mở trình duyệt, ổn định nhất, và **ghép nối hai đoạn 30 giây cũng chỉ dùng được loại tài khoản này**.
Bài này hướng dẫn bạn xuất tài khoản dola đã đăng nhập trong trình duyệt thành Cookie, rồi dán vào trang quản trị.

> ⚠️ Cookie chính là thông tin đăng nhập của tài khoản, ai có nó thì coi như đã đăng nhập vào tài khoản của bạn. **Không gửi vào nhóm chat, không chụp màn hình chia sẻ ra ngoài**.

---

## Ba điều cần biết trước

1. **Quan trọng nhất là `sessionid`**. Nội dung xuất ra bắt buộc phải có mục `sessionid=...`, pool tài khoản dựa vào nó để nhận diện tài khoản và loại bỏ trùng lặp.
   Không có `sessionid` = chưa đăng nhập / xuất nhầm trang web.
2. **Mỗi tài khoản một bản Cookie**. Muốn xuất nhiều tài khoản thì mỗi tài khoản đăng nhập trong **một cửa sổ trình duyệt riêng** (xem mục "Xuất nhiều tài khoản" bên dưới).
3. **Sau khi xuất đừng bấm "Đăng xuất"**. Đăng xuất sẽ làm bản Cookie đó mất hiệu lực ngay lập tức, tài khoản trong pool tài khoản cũng hỏng theo. Dùng xong cứ đóng cửa sổ là được.

---

## Cách 1: Tiện ích Cookie-Editor (đơn giản nhất, khuyên dùng)

1. Tìm và cài tiện ích **Cookie-Editor** (tác giả cgagnier) từ cửa hàng tiện ích của Chrome / Edge.
2. Mở <https://www.dola.com>, đăng nhập tài khoản dola của bạn, xác nhận đã vào tới **trang chat** (thấy được khung hội thoại).
3. Bấm biểu tượng Cookie-Editor ở góc trên bên phải trình duyệt → bấm **Export** → chọn **Header String** (định dạng văn bản header).
   Nội dung sẽ tự động được sao chép vào clipboard, trông như sau (một dòng rất dài):
   ```
   passport_csrf_token=xxx; sid_guard=xxx; sessionid=xxxxxxxx; sid_tt=xxx; ...
   ```
4. Mở trang quản trị của pool tài khoản → "Quản lý tài khoản (账号管理)" → "**＋ Thêm hàng loạt (＋ 批量添加)**" → chọn tab "**Văn bản Cookie header (Cookie 头文本)**".
5. Dán nội dung vừa sao chép vào ô văn bản, **mỗi tài khoản một dòng**; nhiều tài khoản thì xuống dòng rồi dán tiếp. Sau đó bấm [Bắt đầu nhập (开始导入)].

> Ở bước 3 cũng có thể chọn **JSON**: nội dung xuất ra là một mảng `[ {...}, {...} ]`, cũng dán vào ô "Văn bản Cookie header (Cookie 头文本)",
> mỗi mảng là một tài khoản, có thể dán liền nhiều mảng. Với một tài khoản đơn lẻ cũng có thể dùng "＋ Thêm tài khoản (＋ 添加账号)" → tab "Nhập Cookie (Cookie 导入)" để dán JSON.

---

## Cách 2: Không cài tiện ích, dùng công cụ dành cho nhà phát triển của trình duyệt

1. Mở <https://www.dola.com> và đăng nhập, vào tới trang chat.
2. Nhấn **F12** để mở DevTools → chuyển sang tab **Network**.
3. Nhấn **F5** để tải lại trang, bấm vào request đầu tiên ở trên cùng danh sách (tên thường là `www.dola.com` hoặc `chat`).
4. Ở khung bên phải **Headers** → kéo xuống tìm dòng **`cookie:`** trong phần **Request Headers**.
5. Chuột phải lên giá trị của nó → **Copy value** (hoặc tự bôi đen toàn bộ rồi sao chép).
6. Dán vào ô "Văn bản Cookie header (Cookie 头文本)" trong trang quản trị, mỗi dòng một tài khoản.

> ❌ **Không** lấy bằng `document.cookie` trong Console: `sessionid` là HttpOnly, `document.cookie` không lấy được nó,
> tài khoản nhập vào sẽ không hợp lệ.

---

## Xuất nhiều tài khoản

Mỗi tài khoản cần một **phiên trình duyệt không ảnh hưởng lẫn nhau**, chọn một trong các cách sau:

- **Nhiều người dùng (profile) trong trình duyệt (khuyên dùng)**: bấm ảnh đại diện ở góc trên bên phải Chrome → "Thêm" để tạo người dùng mới, mỗi người dùng đăng nhập một tài khoản dola.
  Cookie-Editor cần được cài riêng cho từng người dùng.
- **Cửa sổ ẩn danh**: mỗi lần mở một cửa sổ ẩn danh thì đăng nhập một tài khoản, xuất xong thì **đóng cửa sổ ngay** (không bấm đăng xuất).
  Lưu ý: nhiều cửa sổ ẩn danh mở cùng lúc sẽ dùng chung Cookie, bắt buộc phải **xuất xong một tài khoản, đóng lại, rồi mới mở cửa sổ tiếp theo**.

---

## Sau khi nhập

1. Trong kết quả nhập: `✓ 已创建：acc13、acc14` (đã tạo) = tạo mới thành công (tự đặt tên accN); `↻ 已存在(sessionid 重复)` (đã tồn tại, sessionid trùng) = tài khoản này đã được nhập trước đó, không tạo trùng tài khoản.
2. **Gán proxy** cho tài khoản mới (lối ra Nhật Bản / Hàn Quốc), bấm nút "Proxy (mặc định) (代理(默认))" trên dòng của tài khoản.
   Nhiều tài khoản thì cố gắng chia ra **các IP lối ra khác nhau**: càng nhiều tài khoản dùng chung một lối ra thì càng dễ bị dola đánh giá là "truy cập quá thường xuyên" (710022002).
3. Bấm "**Kiểm tra "xin chào" (你好探测)**": sau khi thông qua, tài khoản chuyển từ [Chờ kích hoạt (待激活)] sang [Đủ hạn mức (满额)], bắt đầu nhận việc.
   Bước kiểm tra sẽ thực sự gửi một câu "你好" (xin chào), bình thường mất vài giây và có thể thấy số ký tự của câu trả lời.

## Tài khoản Doubao (doubao.com)

Pool tài khoản cũng có thể chứa tài khoản Doubao (phục vụ các model `doubao-seedance-*`, xem mục "Mô hình cho tài khoản Doubao (doubao.com)" trong [README](README.vi.md)). Cách xuất giống như trên, chỉ đổi trang web:

1. Đăng nhập trên **www.doubao.com** (bằng số điện thoại Trung Quốc), dùng Cách 1 hoặc Cách 2 để xuất Cookie.
2. Dán vào "Thêm hàng loạt (批量添加) → Cookie header / JSON (Cookie 头 / JSON)":
   - **Cookie JSON** (xuất từ Cookie-Editor) có sẵn tên miền, pool tài khoản tự nhận ra đây là tài khoản Doubao;
   - **Cookie header** (một dòng `a=b; c=d`) không chứa tên miền, trước khi dán hãy chọn mục "Trang web (站点)" bên dưới thành "Doubao doubao.com (豆包 doubao.com)".
3. Sau khi nhập, cột "Nguồn (来源)" của tài khoản hiển thị chữ "Doubao (豆包)" màu xanh dương. Tài khoản Doubao **không cần gán proxy Nhật Bản / Hàn Quốc**, máy chủ ở nước ngoài kết nối trực tiếp là dùng được.
4. "Kiểm tra "xin chào" (你好探测)" của tài khoản Doubao chỉ kiểm tra trạng thái đăng nhập (không gửi tin nhắn, không tốn hạn mức), sau khi thông qua sẽ vào [Đủ hạn mức (满额)] và bắt đầu nhận việc.
5. Lần đầu nhận việc với model dành cho hội viên (2.0 / 2.5), pool tài khoản sẽ tự nhận ra đó là tài khoản "Hội viên (会员)" hay "Tài khoản miễn phí (免费号)" và ghi lại bên dưới cột "Nguồn (来源)".
6. Doubao mặc định gắn watermark hiển thị "豆包AI生成" (do Doubao AI tạo) lên video (cài đặt theo từng tài khoản). Pool tài khoản sẽ tự động tắt giúp khi kiểm tra (`DOUBAO_AUTO_NO_WATERMARK=1`),
   bên dưới cột "Nguồn (来源)" hiển thị "Không watermark ✓ (无水印 ✓)"; khi hiển thị "⚠ Chưa tắt watermark (⚠ 未关水印)" thì bấm "Kiểm tra "xin chào" (你好探测)" một lần, hoặc vào Cài đặt Doubao → Quản lý watermark nội dung AI tạo để tắt thủ công.

Tương tự, **không bấm đăng xuất trong trình duyệt**: thực tế đã thử, sau khi đăng xuất Doubao trong trình duyệt, bản Cookie trong pool tài khoản lập tức báo "Đăng nhập đã hết hạn (登录已过期)".
Muốn đổi tài khoản để đăng nhập thì dùng cửa sổ ẩn danh hoặc một trình duyệt khác.

## Câu hỏi thường gặp

| Hiện tượng | Nguyên nhân / Cách xử lý |
|---|---|
| Kết quả nhập hiển thị "Chưa nhập tài khoản nào (未导入任何账号)" | Nội dung sao chép không có `sessionid`, hoặc thứ sao chép không phải Cookie của dola.com. Hãy xác nhận đã xuất trên trang dola.com và đã đăng nhập. |
| "Kiểm tra "xin chào" (你好探测)" báo bị đăng xuất / chuyển vào [Rủi ro (风控)] | Cookie đã mất hiệu lực: đã bấm đăng xuất trong trình duyệt, đã đổi mật khẩu, hoặc đã xuất quá lâu. Đăng nhập lại và xuất một bản mới, xóa tài khoản cũ rồi nhập lại. |
| Kiểm tra báo "truy cập quá thường xuyên (访问频繁)" (710022002) | Không phải lỗi của Cookie, mà là IP lối ra bị giới hạn tốc độ. Đổi sang proxy có IP khác, hoặc đợi một lát rồi kiểm tra lại. |
| Sau khi nhập, tài khoản cứ nằm ở [Chờ kích hoạt (待激活)] | Chưa được kiểm tra. Gán proxy xong thì bấm "Kiểm tra "xin chào" (你好探测)", hoặc bấm "Kiểm tra "xin chào" (你好探测)" ở phía trên cùng để kiểm tra hàng loạt. |
