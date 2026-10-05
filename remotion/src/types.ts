/**
 * Props cho composition ShortIntro — xem app/short_remotion.py build_props().
 * Audio/cover/music là đường dẫn tuyệt đối do Python truyền vào.
 */
/**
 * Media không phải đường dẫn tuyệt đối: Chrome của Remotion chặn `file://`,
 * nên Python stage file vào public dir và truyền tên file qua đây (đọc bằng
 * staticFile()).
 */
export type ShortIntroProps = {
  /** Tên file audio lời thoại trong public dir. */
  audioSrc: string;
  /** Tên file ảnh bìa trong public dir, rỗng = nền trơn. */
  coverSrc: string;
  /** Tên file nhạc nền trong public dir, rỗng = không nhạc. */
  musicSrc: string;
  width: number;
  height: number;
  fps: number;
  durationInFrames: number;
  bookTitle: string;
  hookText: string;
  cues: Array<{
    text: string;
    start: number;
    end: number;
    words: string[];
  }>;
};

export const defaultProps: ShortIntroProps = {
  audioSrc: "",
  coverSrc: "",
  musicSrc: "",
  width: 1080,
  height: 1920,
  fps: 30,
  durationInFrames: 2250,
  bookTitle: "",
  hookText: "",
  cues: [],
};