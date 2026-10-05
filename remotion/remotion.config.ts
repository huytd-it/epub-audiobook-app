import { Config } from "@remotion/cli/config";

/**
 * FFmpeg thì dùng bản đóng gói kèm Remotion. Chrome Headless Shell được tải về
 * lần đầu (cần mạng); đặt REMOTION_BROWSER_EXECUTABLE trong .env nếu muốn dùng
 * Chrome có sẵn trên máy.
 *
 * concurrency=1: render short chạy song song với job TTS/upload của pipeline
 * sách nói, nên không để Remotion chiếm hết CPU.
 */
Config.setVideoImageFormat("jpeg");
Config.setOverwriteOutput(true);
Config.setConcurrency(1);