# Hướng dẫn vận hành Img2Speech trên Raspberry Pi

Tài liệu này dành cho thiết bị đã được chép code, cài dependency, nối camera,
microphone, loa và ba nút. Nếu chưa hoàn tất phần cài đặt hoặc systemd, xem
[RASPBERRY_PI_DEPLOY_VI.md](RASPBERRY_PI_DEPLOY_VI.md).

## 1. Bảng thao tác nhanh

| Nút | GPIO BCM | Chân vật lý | Thao tác |
|---|---:|---:|---|
| Nút 1 | GPIO 17 | Pin 11 | Nhấn ngắn để chụp khi chưa có trang, đang đọc hoặc đã đọc xong; sau tóm tắt/trả lời thì nhấn ngắn để đọc tiếp. Giữ 1,5 giây để luôn bỏ trang hiện tại và chụp trang mới |
| Nút 2 | GPIO 27 | Pin 13 | Dừng âm thanh hiện tại và tóm tắt phần đã nghe |
| Nút 3 | GPIO 22 | Pin 15 | Nhấn lần một để bắt đầu hỏi; nhấn lần hai để kết thúc và nhận câu trả lời |

Đầu còn lại của cả ba nút nối GND, ví dụ chân vật lý 6. Chương trình dùng số
GPIO theo chuẩn **BCM**, không dùng số chân vật lý làm tham số phần mềm.
Tắt nguồn trước khi sửa dây; nút chỉ nối GPIO với GND, không nối với chân 3,3 V
hoặc 5 V.

## 2. Kiểm tra trước khi bật thiết bị

1. Đặt camera chắc chắn, lau sạch ống kính và bảo đảm trang sách đủ sáng.
2. Bật loa, cắm microphone và kiểm tra Raspberry Pi có Internet.
3. Bảo đảm chỉ chạy một bản Img2Speech. Không chạy lệnh thủ công đồng thời với
   service systemd vì hai tiến trình sẽ tranh GPIO, camera, microphone và loa.
4. Nếu chạy bằng service, kiểm tra:

   ```bash
   systemctl status img2speech.service --no-pager
   ```

5. Trạng thái `active (running)` là dấu hiệu service đã chạy. Chương trình không
   phát lời chào lúc khởi động; log có thể hiển thị dòng
   `Three-button reader ready`.

## 3. Khởi động chương trình

### Cách A — service tự chạy cùng Raspberry Pi

Khởi động hoặc khởi động lại service:

```bash
sudo systemctl start img2speech.service
sudo systemctl restart img2speech.service
systemctl status img2speech.service --no-pager
```

Theo dõi log trực tiếp khi thử thiết bị:

```bash
journalctl -u img2speech.service -f
```

Nhấn `Ctrl+C` chỉ thoát màn hình log, không dừng service.

### Cách B — chạy thủ công để kiểm tra lỗi

Trước tiên dừng service để tránh chạy hai bản:

```bash
sudo systemctl stop img2speech.service
cd /home/pi/Img2Speech
uv run device_reader.py \
  --capture-command 'rpicam-still --nopreview --timeout 1500 --output {output}' \
  --record-command 'arecord -q -t wav -f S16_LE -r 16000 -c 1 {output}'
```

Ba GPIO mặc định là 17, 27 và 22; giọng đọc mặc định là `marin`. Nếu thư mục
dự án hoặc thiết bị ALSA khác, dùng đúng đường dẫn/lệnh đã cấu hình cho máy.
Không chạy chương trình bằng `sudo` hoặc tài khoản root.

## 4. Đọc một trang mới

1. Đặt một trang duy nhất trước camera, giữ trang phẳng và không che nội dung.
2. Nhấn ngắn Nút 1.
3. Chờ lần lượt các thông báo:
   - “Đang chụp ảnh.”
   - “Đang xử lý ảnh.”
   - “Bắt đầu đọc.”
4. Giữ nguyên trang cho tới khi nghe “Đang xử lý ảnh.”
5. Hệ thống tự OCR và bắt đầu đọc ngay khi có câu đầu tiên; không cần nhấn thêm
   nút và không phải đợi xử lý xong toàn bộ trang.
6. Khi hết trang, thiết bị nói “Đã đọc xong.” Nhấn ngắn Nút 1 để chụp trang kế
   tiếp.

Lưu ý: khi hệ thống đang đọc, một lần nhấn ngắn Nút 1 cũng chuyển sang chụp
trang mới. Không nhấn Nút 1 nếu chỉ muốn tiếp tục nghe trang hiện tại.

## 5. Tóm tắt rồi đọc tiếp

1. Trong lúc đang nghe, nhấn Nút 2.
2. Âm thanh hiện tại dừng và thiết bị nói “Bắt đầu tóm tắt.”
3. Chờ bản tóm tắt, sau đó thiết bị nói “Tóm tắt xong.”
4. Nhấn ngắn Nút 1 để tiếp tục từ đầu câu đã bị ngắt.

Bản tóm tắt chỉ dựa trên các câu gốc đã nghe hoàn chỉnh. Nếu bị ngắt ngay ở
câu đầu tiên, hệ thống dùng câu đầu đã bắt đầu làm dữ liệu dự phòng. Nội dung
tóm tắt không được đưa ngược vào trang sách.

## 6. Đặt câu hỏi về phần đã nghe

1. Nhấn Nút 3 lần thứ nhất. Thiết bị dừng âm thanh, nói “Bắt đầu ghi câu hỏi”
   rồi phát **một tiếng beep**.
2. Chỉ bắt đầu nói sau tiếng beep để phần đầu câu hỏi không bị mất.
3. Nhấn Nút 3 lần thứ hai khi nói xong. Recorder dừng trước, sau đó thiết bị
   phát **hai tiếng beep** và nói “Đã ghi xong. Đang xử lý câu hỏi.”
4. Chờ thông báo “Bắt đầu trả lời”, nghe câu trả lời, rồi chờ “Đã trả lời xong.”
5. Nhấn ngắn Nút 1 để đọc tiếp trang đang dở, hoặc nhấn Nút 3 để hỏi câu mới.

Nếu không nhấn lần hai, câu hỏi tự dừng sau 60 giây. Mỗi câu hỏi chỉ dùng phần
văn bản gốc đã nghe của trang hiện tại và không mang theo lịch sử hỏi đáp cũ.
Nếu nhấn Nút 3 trong lúc hệ thống vẫn xử lý câu hỏi, thiết bị báo “Đang xử lý
câu hỏi. Vui lòng chờ.”

Trong lúc đang ghi, hãy kết thúc bằng Nút 3. Nhấn Nút 1 sẽ bỏ bản ghi câu hỏi;
nhấn Nút 2 sẽ bỏ bản ghi và chuyển sang tóm tắt.

## 7. Bỏ trang đang dở và chuyển sang trang mới

1. Đặt **trang mới** ổn định trước camera trước khi bấm nút.
2. Nhấn và giữ Nút 1 khoảng **1,5 giây**, cho tới khi nghe “Đang chụp ảnh.”
3. Khi thiết bị nhận thao tác giữ, nó dừng audio/ghi âm, hủy OCR hoặc tác vụ
   model cũ, xóa trang hiện tại và nói “Đang chụp ảnh.”
4. Nhả nút, giữ nguyên trang cho tới khi nghe “Đang xử lý ảnh”, sau đó chờ hệ
   thống OCR rồi tự động đọc.

Thao tác giữ chỉ phát một lệnh chụp mới; khi nhả nút, hệ thống không đồng thời
đọc tiếp trang đã bỏ. Trang cũ, con trỏ, tóm tắt và câu hỏi/trả lời gần nhất bị
xóa. Nếu camera chụp thất bại, trang cũ vẫn được xem là đã bỏ; sửa lỗi camera
rồi nhấn Nút 1 để thử chụp lại.

Khi chủ động đổi trang, nên dùng thao tác giữ 1,5 giây để kết quả không phụ
thuộc trạng thái hiện tại. Thao tác này không thể hoàn tác.

## 8. Ý nghĩa thông báo và tiếng beep

| Tín hiệu | Ý nghĩa |
|---|---|
| “Bắt đầu đọc” / “Tiếp tục đọc” | Đang phát nội dung trang mới / trang đang dở |
| “Đã đọc xong” | Đã hết nội dung trang hiện tại |
| Một beep sau lời báo ghi âm | Microphone bắt đầu ghi; có thể nói câu hỏi |
| Hai beep trước lời báo xử lý | Microphone đã dừng; hệ thống bắt đầu xử lý câu hỏi |
| Ba beep thấp kèm lời báo | Có lỗi camera, microphone, model hoặc loa |
| Chỉ có ba beep thấp | Không tạo được lời báo bằng TTS; xem log để biết lỗi gốc |

Lời báo được tạo bằng cùng model/giọng `marin` và cache trong RAM. Lần phát đầu
tiên của một thông báo có thể chậm hơn các lần sau.

## 9. Dừng và tắt thiết bị an toàn

Nếu chạy thủ công, nhấn `Ctrl+C` tại terminal. Nếu chạy bằng service:

```bash
sudo systemctl stop img2speech.service
```

Khi cần tắt nguồn Raspberry Pi:

```bash
sudo poweroff
```

Chờ hệ điều hành tắt hoàn toàn rồi mới ngắt nguồn. Không rút nguồn trong lúc
camera, microphone hoặc hệ thống đang ghi dữ liệu.

## 10. Xử lý lỗi nhanh

### Nhấn nút không có phản hồi

- Kiểm tra service và log:

  ```bash
  systemctl status img2speech.service --no-pager
  journalctl -u img2speech.service -n 100 --no-pager
  ```

- Kiểm tra đúng dây GPIO17/27/22 nối qua nút xuống GND.
- Kiểm tra user chạy service thuộc group `gpio` và có quyền dùng
  `/dev/gpiochip*`.
- Nếu nhấn giữ không đổi trang, giữ liên tục ít nhất 1,5 giây rồi mới nhả.
- Nếu status báo `203/EXEC`, tìm đường dẫn thật của `uv` bằng `command -v uv`,
  sửa `ExecStart` trong service theo đường dẫn đó rồi chạy `daemon-reload` và
  restart theo hướng dẫn triển khai.

### Không chụp được ảnh

```bash
rpicam-still --nopreview --timeout 1500 --output /tmp/img2speech-test.jpg
file /tmp/img2speech-test.jpg
```

Kiểm tra cáp camera, ánh sáng và command phải xuất JPEG. Sau khi sửa lỗi, nhấn
Nút 1 để chụp lại.

### Không nghe tiếng

```bash
aplay -l
```

Kiểm tra loa, âm lượng bằng `alsamixer`, sound card mặc định và group `audio`
của user chạy service.

### Không ghi hoặc không nhận câu hỏi

```bash
arecord -q -t wav -f S16_LE -r 16000 -c 1 -d 5 /tmp/img2speech-mic.wav
aplay /tmp/img2speech-mic.wav
```

Kiểm tra microphone, sound card thu âm và nhớ chỉ nói sau một tiếng beep.

### Báo lỗi model hoặc ba beep thấp

- Kiểm tra Internet và `OPENAI_API_KEY` trong `.env`; không in hoặc sao chép khóa
  API vào log/tin nhắn.
- Xem lỗi chi tiết:

  ```bash
  journalctl -u img2speech.service -n 100 --no-pager
  ```

## 11. Checklist vận hành hằng ngày

- Service ở trạng thái `active (running)` và không bị restart liên tục.
- Camera nhìn trọn trang, đủ sáng, không rung và không bị lóa.
- Loa nghe rõ; microphone thu được giọng nói.
- Ba nút phản hồi đúng: Nút 1 đọc/chụp, Nút 2 tóm tắt, Nút 3 hỏi đáp.
- Thử giữ Nút 1 để chắc chức năng đổi trang mới hoạt động.
- Khi có lỗi, ghi lại thời điểm và lấy log bằng `journalctl` trước khi restart.

Khởi động lại service, khởi động lại Raspberry Pi hoặc mất điện sẽ làm mất trang,
tiến độ đọc và ngữ cảnh hiện tại. Tránh bấm liên tục khi thiết bị đang phát lời
báo hoặc đang xử lý; các thao tác có thể được xếp hàng và phản hồi muộn.

Ảnh, âm thanh câu hỏi và văn bản OCR được gửi đến OpenAI để xử lý và có thể
phát sinh chi phí API. Chỉ vận hành với nội dung mà người dùng cho phép xử lý.
