Local Reader App

Cach dung tren may moi:
1. Bam "Setup Local Reader.bat" mot lan de tao runtime trong %LOCALAPPDATA%\LocalReaderApp.
2. Bam "Local Reader.bat" hoac shortcut Local Reader de mo http://127.0.0.1:8765.
3. Upload PDF/DOCX/TXT roi bam Doc hoac Xu ly.
4. Khi khong dung nua, bam "Stop Local Reader.bat".

Ghi chu:
- Code va du lieu app nam trong folder nay.
- Runtime Python/VieNeu nam rieng theo tung may tai %LOCALAPPDATA%\LocalReaderApp, khong phu thuoc user ASUS/ifroy.
- May co NVIDIA se dung VieNeu CUDA/GGUF neu runtime ho tro.
- May khong co NVIDIA van dung VieNeu/torch CPU de chay duoc.
- Audio/cache doc van luu trong reader_audio_cache cua folder app nay.
- Neu OCR PDF scan tieng Viet can cai them vie.traineddata cho Tesseract.
