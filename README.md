# PNG Mockup

<p align="center">
  <img src="App%20Source/assets/logo.png" width="144" height="144" alt="PNG Mockup logo">
</p>

Ứng dụng Windows độc lập để ghép thiết kế PNG vào mockup PSD/PSB bằng Photoshop.
Mã nguồn, bộ chạy, cấu hình và dữ liệu của dự án này không dùng chung với app gốc.

## Bản chạy và bộ cài

Tải bộ cài chính thức tại [GitHub Releases](https://github.com/minhtuan5991/PNG-Mockup/releases/latest).

- `PNG Mockup Setup.exe`: bộ cài offline để chuyển sang máy khác.
- `App Source/PNG Mockup.exe`: mở bản chạy trong dự án.
- `App Source/README.md`: hướng dẫn sử dụng đi kèm bộ cài.
- `verification/`: kết quả kiểm thử và đối chiếu bản đóng gói.

Bộ cài kèm Python/Tk, Pillow, pngquant, oxipng và libwebp riêng. Không cần cài
sẵn Python, app cũ, trình duyệt hoặc các model AI. Máy đích vẫn cần Adobe
Photoshop có Automation/COM hoạt động. Không có bản quyền hoặc bộ cài Photoshop
đi kèm.

## Cấu trúc phát triển

`App Source` là nguồn chính. `installer` và `build_setup.ps1` tạo bộ cài. `tests`
chứa kiểm thử PNG/PSD, xử lý ảnh, giao diện và đóng gói. Không dùng script đồng bộ,
runtime, cấu hình hoặc kho cập nhật của dự án cũ.

Khi thử bộ cài trên máy phát triển, chọn thư mục cha khác với checkout hiện tại.
Bộ cài không cho cài đè vào thư mục mã nguồn hoặc `App Source`.

Chỉ có một phong cách giao diện và năm bảng màu. Từ 1.1.0, bộ cài tạo thư mục
`PNG Mockup` dưới ổ đĩa/thư mục cha đã chọn, ẩn file kỹ thuật và kèm công cụ
hiện/ẩn cùng Uninstall độc lập.

Từ 1.1.1, logo áo chữ E được dùng đồng nhất cho cửa sổ, file chạy, bộ cài và
tài sản favicon; các thao tác thư mục chính có chữ đậm và nhóm bảo trì có viền
xanh nhạt theo giao diện mới.

Từ 1.1.2, bản cài đăng ký PNG Mockup theo người dùng hiện tại để ứng dụng xuất
hiện trong Control Panel và Windows Settings. Cập nhật thay đổi version/dung
lượng tại cùng mục.

Từ 1.1.3, `Uninstall.exe` dùng icon thùng rác đỏ và
`Show technical files.exe` dùng icon bánh răng riêng. Gỡ cài đặt xóa chương
trình, cấu hình, logs, cache cập nhật, file tạm riêng, shortcut và đăng ký
Windows; chỉ giữ ba thư mục `PNG`, `Mockup`, `Output` cùng nội dung bên trong.

Từ 1.1.4, nút **Nén ảnh PNG** dùng một luồng tự động cho in POD. App nhắm giảm
khoảng 35–40%, giữ kích thước, alpha, ICC/DPI và chỉ nhận kết quả qua cổng kiểm
tra chất lượng; ảnh không thể đạt mục tiêu sẽ dùng kết quả an toàn hơn.

Từ 1.1.5, luồng nén chính dùng palette thích ứng tối đa 256 màu với alpha và
oxipng. Ảnh mẫu 4200×4800 giảm từ 12.005.218 xuống 3.670.636 byte (69,42%), nhỏ
hơn kết quả iLoveIMG 4.157.821 byte trong khi PSNR đo trên nền trắng cao hơn.
Ảnh không phù hợp palette tự quay về near-lossless/lossless; giao diện vẫn chỉ
có một lựa chọn **Nén ảnh PNG**.

Updater kiểm tra kênh `minhtuan5991/PNG-Mockup` mỗi lần mở app đã cài và tải nền,
không ngắt job. Bản chạy từ checkout mã nguồn không tự cập nhật đè mã nguồn.
Cài cập nhật cần người dùng chọn khi app rảnh, rồi chờ phiên hiện tại đóng.
Kênh chưa được cấu hình sẽ không kết nối mạng; không dùng kho của ứng dụng gốc.

## Mã nguồn và phát hành

Cấu hình cá nhân, ảnh, log, runtime và EXE sinh ra không nằm trong Git. Bản cài
đóng gói cấu hình mới từ `payload-template/config.json`, không lấy cài đặt cá nhân
trong `App Source/config.json`.

Để chạy một checkout mới, sao chép template thành `App Source/config.json`,
chuẩn bị runtime CPython 3.12 64-bit có Tk/Pillow bằng `tools/prepare_runtime.ps1`,
sau đó chạy `Build Setup.bat`. Runtime chỉ được đưa vào release asset.

Xem [quy trình phát hành](docs/RELEASING.md). Không lưu token GitHub vào nguồn,
runtime hoặc bộ cài; công cụ phát hành dùng Git Credential Manager của máy phát triển.
