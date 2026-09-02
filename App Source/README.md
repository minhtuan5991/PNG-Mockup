# PNG Mockup 1.1.6

Tiện ích **Nén ảnh PNG** dùng một luồng tự động duy nhất cho POD. Từ 1.1.5,
luồng chính chuyển ảnh 8-bit phù hợp sang palette tối đa 256 màu có alpha rồi
tối ưu lossless lần cuối. Ảnh gốc luôn được giữ nguyên.

Tạo mockup từ PNG bằng Photoshop, với bộ chạy và dữ liệu riêng trong thư mục app.

## Cài trên máy khác

1. Chép `PNG Mockup Setup.exe` sang máy Windows 10/11 64-bit.
2. Chạy bộ cài và chọn ổ đĩa hoặc thư mục cha có quyền ghi. Bộ cài tự tạo thư mục
   `PNG Mockup` bên trong và hiển thị đường dẫn đích trước khi cài. Ví dụ, chọn
   `E:\Tools` sẽ cài vào `E:\Tools\PNG Mockup`. Mặc định dùng thư mục Programs
   của tài khoản Windows hiện tại. Không chọn thư mục chứa dự án nguồn/app khác.
3. Mở PNG Mockup bằng file chạy hoặc shortcut của ứng dụng.
4. Máy cần Adobe Photoshop có Automation/COM hoạt động. Mở Photoshop một lần,
   hoàn tất khởi tạo/bản quyền nếu có, rồi dùng **Kiểm tra hệ thống** trong tool.

Bộ cài chứa Python, Tk, Pillow, pngquant, oxipng và libwebp; không cần mạng để
cài những thành phần này, không cần app cũ hoặc Python cài sẵn. Photoshop không
nằm trong bộ cài.

## Bắt đầu

1. Đặt thiết kế `.png` vào `PNG`.
2. Đặt PSD/PSB đã chuẩn bị Smart Object vào `Mockup`.
3. Dùng **Đổi tên** nếu cần bổ sung marker theo quy tắc bên dưới.
4. Bấm **Chạy thử 1 job**, kiểm tra ảnh trong `Output`, rồi **Bắt đầu / Tiếp tục**.

| Loại | Quy tắc giữ nguyên |
| --- | --- |
| PSD/PSB một mặt | Có `.1S` trước đuôi file, ví dụ `shirt.1S.psd` |
| PSD/PSB hai mặt | Có `.2S`, ví dụ `shirt.2S.psb` |
| Biến thể thiết kế | Giữ các marker `.wh` và `.bl` như bộ thiết kế đang dùng |
| PNG trước/sau | Giữ `.f` và `.b`, kết hợp marker màu khi cần |
| Smart Object | Giữ `Design Dark`, `Design Light` và các tên trước/sau được hỗ trợ |

PNG hai mặt vẫn hỗ trợ trường hợp ba file dùng chung một mặt. Hệ thống cảnh báo
nhóm thiếu biến thể theo quy tắc cũ. PNG không ghép với mockup ảnh raster và không
có luồng tạo ảnh bằng trình duyệt. Các group màu áo trong PSD được xuất riêng.

## Vận hành và kết quả

- Job chạy tuần tự qua Photoshop, không có giới hạn 20 job hoặc nghỉ batch.
- **Chạy thử 1 job** chỉ xử lý một job. **Chạy lại lỗi** chỉ xử lý các job lỗi.
- **Dừng sau job hiện tại** cho phép Photoshop hoàn thành job đang chạy.
- **Buộc dừng** có thể ngắt một job đang xử lý; ưu tiên dừng sau job hiện tại.
- Queue, trạng thái và nhật ký nằm trong `logs`; tool tiếp tục các job chưa xong.
- Photoshop xuất theo canvas/DPI của PSD. Các trường kích thước/DPI trong cài đặt
  dùng để kiểm tra watermark; chúng không tự resize kết quả PSD.
- Watermark mặc định tắt; xóa metadata mặc định bật. PNG watermark phải tĩnh,
  có nền trong suốt và khớp kích thước/DPI đã cấu hình.
- Khi chạy lại, kết quả PSD trùng tên có thể được thay thế như quy tắc cũ. Hãy
  sao lưu `Output` trước khi bật **Chạy lại job đã hoàn tất**.

Không chạy nhiều lượt tự động hóa PSD cùng lúc trên cùng một phiên Photoshop.
Đóng hoặc lưu các tài liệu Photoshop cá nhân trước khi chạy batch lớn.

## Công cụ ảnh

Giữ các tiện ích đổi tên PNG/Mockup, thay đổi kích thước/nén PNG, xóa metadata và
gắn watermark thủ công. Nút mở tiện ích có tên **Chọn ảnh để xử lý**; tùy chọn
bên trong vẫn là **Nén ảnh PNG**. Chế độ nén được bật sẵn và tự dùng palette thích
ứng có cổng kiểm tra chất lượng, không có thanh chất lượng hoặc nhiều preset.
Công cụ giữ nguyên kích thước, ICC và DPI;
palette có thể giảm số màu và mức alpha để đổi lấy dung lượng nhỏ hơn, tương tự
nhóm dịch vụ nén PNG trực tuyến. Metadata văn bản không ảnh hưởng in có thể được
loại bỏ.

Mục tiêu tối thiểu vẫn là giảm 35%; ảnh RGBA nhiều màu phù hợp thường có thể giảm
60–70%, nhưng tỷ lệ thực tế phụ thuộc nội dung và mức tối ưu của ảnh nguồn. Nếu
palette không đạt cổng chất lượng, app tự dùng near-lossless an toàn hơn hoặc
fallback lossless thay vì ép giảm dung lượng. Nhật ký hoàn tất hiển thị tỷ lệ
thực tế, số ảnh đạt mục tiêu và số ảnh phải fallback. Resize cũ vẫn có thể dùng
riêng hoặc kết hợp với nén.

Tiện ích resize/nén không phải luồng xóa nền. Tiện ích thủ công tạo bản kết quả
riêng, giữ ảnh nguồn và xử lý tên trùng theo quy tắc cũ.

## Giao diện và dữ liệu riêng

Có một phong cách giao diện với năm bảng màu: Mint, Tím nhẹ, Sáng, Tối và Nâu.
Mặc định Tím nhẹ. Cấu hình được lưu trong `config.json` của chính app này.

Không có Idea 1S, Idea 2S, xóa nền, đăng nhập Edge, PrintArea hoặc sửa Prompt.
Không tải model AI, lưu tài khoản hay kết nối kho cập nhật của app cũ.

Cài lại bằng bộ cài PNG Mockup vào đúng thư mục sẽ giữ cấu hình và các thư mục
`PNG`, `Mockup`, `Output`, `logs`. Nếu chọn đúng thư mục đã cài, bộ cài nhận diện
bản hiện có và không tạo thêm một thư mục `PNG Mockup` lồng bên trong. Vẫn nên
sao lưu dữ liệu quan trọng trước khi cập nhật.

Sau khi cài, **PNG Mockup** xuất hiện trong **Control Panel → Programs and
Features** và **Settings → Installed apps** của tài khoản Windows hiện tại.
Mục này hiển thị phiên bản, nhà phát hành, dung lượng và gọi đúng `Uninstall.exe`
trong thư mục app. Cập nhật app sẽ cập nhật thông tin này; gỡ app thành công sẽ
xóa mục tương ứng nhưng vẫn giữ dữ liệu theo quy tắc bên dưới.

## Thư mục gọn và gỡ cài đặt

Bản cài thông thường chỉ hiện:

- `PNG Mockup.exe`: khởi động ứng dụng.
- `Uninstall.exe`: icon thùng rác đỏ; gỡ sạch app sau khi bạn xác nhận.
- `Show technical files.exe`: icon bánh răng; hiện/ẩn các file kỹ thuật của app.
- `PNG`, `Mockup`, `Output`: các thư mục làm việc.

Runtime, mã nguồn, cấu hình, nhật ký và các file hỗ trợ vẫn nằm trong thư mục
app nhưng mang thuộc tính Hidden. Nếu Explorer đang bật hiển thị file ẩn, bạn
vẫn có thể thấy chúng. Tool không đổi thiết lập Explorer toàn hệ thống.

Trước khi gỡ, đóng PNG Mockup. Uninstall chỉ giữ nguyên ba thư mục `PNG`,
`Mockup`, `Output` cùng toàn bộ nội dung bên trong. `config.json`, `logs`, runtime,
cache cập nhật, file kỹ thuật, file thêm/sửa khác trong thư mục app và thư mục
tạm riêng của bản cài trên ổ C đều bị xóa. Shortcut và mục PNG Mockup trong
Control Panel/Installed apps cũng bị xóa. Gỡ app không gỡ Photoshop.

Sau khi gỡ, có thể cài lại vào thư mục `PNG Mockup` chỉ còn ba thư mục làm việc;
bộ cài nhận đúng vị trí và không tạo thêm thư mục lồng. Các cài đặt cá nhân đã
bị xóa sẽ trở về mặc định ở lần cài lại.

## Cập nhật ngầm

Bộ cài phát hành sử dụng kênh GitHub riêng `minhtuan5991/PNG-Mockup`. Mỗi lần
khởi động app đã cài sẽ kiểm tra bản mới ở chế độ nền. Không có bản mới hoặc
mất mạng sẽ không hiện hộp thoại.
Chỉ khi phát hiện phiên bản mới hơn mới hiện thông báo nhỏ và bắt đầu tải ngầm;
tác vụ PNG/Photoshop đang chạy được giữ nguyên.

Sau khi tải xong, app đối chiếu SHA-256, định danh sản phẩm, phiên bản, kênh và
các file trong bộ cài. Bấm **Cài và mở lại** khi mọi tác vụ đã hoàn tất. Bộ cài
đợi phiên hiện tại đóng rồi mới thay chương trình; không buộc dừng Photoshop.
Cài đặt và dữ liệu được giữ lại. Đóng app giữa lúc tải sẽ hủy lượt tải chưa xong
và có thể tải lại ở lần mở sau.

Kênh cập nhật nằm trong `update-channel.json` đi kèm sản phẩm, không lấy từ
thiết lập repository/chu kỳ cũ trong `config.json`. App không chứa tài khoản hay
token GitHub và không dùng kho cập nhật của ứng dụng gốc. Nếu kênh chưa cấu hình,
**Cài đặt** sẽ hiển thị trạng thái đó; vẫn dùng bộ cài riêng để cập nhật thủ công.

**Lưu ý chuyển bản:** bản 1.0.0 và bản 1.1.0 nội bộ chưa bật kênh GitHub cần
chạy bộ cài phát hành này một lần. Sau đó, các phiên bản mới hơn được phát hiện
tự động. Tải bộ cài từ [GitHub Releases](https://github.com/minhtuan5991/PNG-Mockup/releases/latest).

Bản chạy trực tiếp trong checkout `App Source` không tự cập nhật đè mã nguồn.
Hãy dùng bản đã cài ở thư mục riêng để nhận các cập nhật tự động.

## Xử lý lỗi

**Không có job:** kiểm tra PNG đầu vào và marker `.1S/.2S` của PSD/PSB. Nhấn tạo
lại hàng đợi sau khi sửa tên hoặc bổ sung file.

**Không kết nối được Photoshop:** mở Photoshop bằng cùng tài khoản Windows,
hoàn tất khởi tạo, đóng rồi thử lại. Nếu COM chưa đăng ký, sửa/cài lại Photoshop
bằng công cụ chính thức của Adobe. Tool không tự sửa registry Photoshop.

**Sai watermark:** chọn PNG tĩnh có alpha, đúng kích thước/DPI. Khi lỗi xử lý ảnh,
job báo lỗi và không thay thế kết quả bằng một file chưa qua kiểm tra.

**Gặp cảnh báo Windows:** bộ cài nội bộ chưa có chữ ký số của nhà phát hành.
Không tắt các biện pháp bảo vệ Windows; chỉ dùng file từ nguồn bạn tin cậy và
đối chiếu SHA-256 đi kèm khi cần.
