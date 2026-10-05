/**
 * ShortIntro — lớp đồ hoạ dọc cho video giới thiệu truyện.
 *
 * Chỉ lo phần hình ảnh: cover nền, hook 3 giâu, sub highlight theo từ và thanh
 * tiến. Giọng đọc và nhạc nền do Python (ffmpeg/TTS) chuẩn bị sẵn rồi đưa vào.
 */
import React from "react";
import {
  AbsoluteFill,
  Audio,
  Img,
  interpolate,
  spring,
  staticFile,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import type { ShortIntroProps } from "./types";

const HOOK_SECONDS = 3;
/** Khung hình cuối cùng được tô sáng để câu không bị cắt giữa chừng. */
const HIGHLIGHT_LEAD = 2;
/**
 * Font sans có dấu tiếng Việt. Không load font từ máy/ngoài mạng nên phải là
 * asset trong public dir: mọi browser đều có file này (Windows/macOS đều có
 * Arial) và nó đủ dấu tiếng Việt. Python stage kèm file khi tồn tại.
 */
const FONT_STACK =
  '"Be Vietnam Pro", "Inter", "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif';

const Karaoke: React.FC<{ cue: ShortIntroProps["cues"][number]; color: string }> = ({
  cue,
  color,
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  const startFrame = Math.round(cue.start * fps);
  const endFrame = Math.round(cue.end * fps);
  const elapsed = frame - startFrame;
  const total = Math.max(1, endFrame - startFrame);
  // Từ nào đang được đọc tới: highlight chạy đều theo số từ của cue.
  const active = Math.floor((elapsed / total) * cue.words.length);
  const done = elapsed >= 0;
  const dim = interpolate(elapsed, [0, 6], [0.45, 0.72], {
    extrapolateRight: "clamp",
  });

  if (frame < startFrame - HIGHLIGHT_LEAD || frame > endFrame) return null;

  return (
    <div
      style={{
        display: "flex",
        flexWrap: "wrap",
        justifyContent: "center",
        gap: "0 14px",
        padding: "0 70px",
      }}
    >
      {cue.words.map((word, index) => (
        <span
          key={`${word}-${index}`}
          style={{
            fontFamily: FONT_STACK,
            fontSize: 62,
            fontWeight: 800,
            lineHeight: 1.22,
            color:
              done && index <= active ? color : `rgba(255,255,255,${dim})`,
            textShadow: "0 6px 24px rgba(0,0,0,0.85), 0 2px 4px rgba(0,0,0,0.9)",
            textTransform: "none",
          }}
        >
          {word}
        </span>
      ))}
    </div>
  );
};

export const ShortIntro: React.FC<ShortIntroProps> = (props) => {
  const frame = useCurrentFrame();
  const { fps, durationInFrames } = useVideoConfig();
  const width = props.width || 1080;
  const height = props.height || 1920;

  const hookFrames = HOOK_SECONDS * fps;
  const progress = Math.min(1, frame / Math.max(1, durationInFrames - 1));
  // Hook vào bằng spring, ra bằng fade — đủ để giữ 3 giây dễ đọc.
  const hookIn = spring({ frame, fps, config: { damping: 200 } });
  const hookOpacity = interpolate(
    frame,
    [0, 10, hookFrames - 20, hookFrames],
    [0, 1, 1, 0],
    { extrapolateLeft: "clamp", extrapolateRight: "clamp" },
  );

  return (
    <AbsoluteFill style={{ backgroundColor: "#05070f", overflow: "hidden" }}>
      {props.coverSrc ? (
        <AbsoluteFill style={{ opacity: 0.5 }}>
          <Img
            src={staticFile(props.coverSrc)}
            style={{
              width: "100%",
              height: "100%",
              objectFit: "cover",
              // Ken Burns rất chậm để ảnh bìa không bị "tĩnh" như slideshow.
              transform: `scale(${interpolate(progress, [0, 1], [1.04, 1.14])})`,
            }}
          />
        </AbsoluteFill>
      ) : null}

      <AbsoluteFill
        style={{
          background:
            "linear-gradient(180deg, rgba(5,7,15,0.85) 0%, rgba(5,7,15,0.35) 32%, rgba(5,7,15,0.45) 62%, rgba(5,7,15,0.95) 88%)",
        }}
      />

      {props.audioSrc ? <Audio src={staticFile(props.audioSrc)} /> : null}
      {props.musicSrc ? <Audio src={staticFile(props.musicSrc)} volume={0.15} /> : null}

      {frame < hookFrames ? (
        <AbsoluteFill
          style={{
            justifyContent: "center",
            alignItems: "center",
            padding: "0 90px",
            opacity: hookOpacity,
            transform: `scale(${0.92 + hookIn * 0.08})`,
          }}
        >
          <div
            style={{
              fontFamily: FONT_STACK,
              fontSize: 96,
              fontWeight: 900,
              lineHeight: 1.1,
              textAlign: "center",
              color: "#ffffff",
              textShadow: "0 10px 40px rgba(0,0,0,0.9)",
            }}
          >
            {props.hookText || props.bookTitle}
          </div>
        </AbsoluteFill>
      ) : null}

      <AbsoluteFill
        style={{
          justifyContent: "flex-end",
          alignItems: "center",
          paddingBottom: 320,
        }}
      >
        {props.cues.map((cue, index) => (
          <Karaoke key={`${cue.start}-${index}`} cue={cue} color="#ffe066" />
        ))}
      </AbsoluteFill>

      {/* Thanh tiến + tên sách: neo thị giác cuối khung hình, quen thuộc với short dọc. */}
      <div
        style={{
          position: "absolute",
          left: 70,
          right: 70,
          bottom: 150,
          height: 8,
          borderRadius: 4,
          backgroundColor: "rgba(255,255,255,0.18)",
        }}
      >
        <div
          style={{
            width: `${progress * 100}%`,
            height: "100%",
            borderRadius: 4,
            backgroundColor: "#ffe066",
          }}
        />
      </div>
      {props.bookTitle ? (
        <div
          style={{
            position: "absolute",
            left: 70,
            right: 70,
            bottom: 92,
            fontFamily: FONT_STACK,
            fontSize: 34,
            fontWeight: 700,
            color: "rgba(255,255,255,0.82)",
            textShadow: "0 3px 12px rgba(0,0,0,0.9)",
            overflow: "hidden",
            whiteSpace: "nowrap",
            textOverflow: "ellipsis",
          }}
        >
          {props.bookTitle}
        </div>
      ) : null}
      <div style={{ width: width, height: height, position: "absolute" }} />
    </AbsoluteFill>
  );
};