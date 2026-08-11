import React, { useMemo } from "react";
import { cn } from "@/lib/utils";
import { Span } from "./types";

const SPAN_STYLE: Record<string, string> = {
  error: "bg-red-200 text-red-900 ring-1 ring-red-400",
  warning: "bg-amber-200 text-amber-900",
  info: "bg-sky-100 text-sky-900",
};

// Không glyph nào để nhìn thấy — hiện chấm thay thế cho ký tự vô hình/điều khiển,
// nếu không thì highlight sẽ vô hình đúng như lỗi nó đang chỉ ra.
const INVISIBLE_CODES = new Set(["zero_width", "control_chars"]);

const MAX_RENDER_CHARS = 120_000;

type Piece = { text: string; span?: Span; key: string };

function buildPieces(text: string, spans: Span[]): Piece[] {
  const pieces: Piece[] = [];
  let cursor = 0;
  let key = 0;

  for (const span of spans) {
    const start = Math.max(cursor, Math.min(span.start, text.length));
    const end = Math.max(start, Math.min(span.start + span.length, text.length));
    if (end <= cursor) continue; // lệch/overlap từ payload cũ -> bỏ qua thay vì vỡ layout
    if (start > cursor) pieces.push({ text: text.slice(cursor, start), key: String(key++) });
    pieces.push({ text: text.slice(start, end), span, key: String(key++) });
    cursor = end;
  }
  if (cursor < text.length) pieces.push({ text: text.slice(cursor), key: String(key++) });
  return pieces;
}

export function HighlightedText({
  text,
  spans,
  activeCode,
  onSpanClick,
}: {
  text: string;
  spans: Span[];
  activeCode?: string;
  onSpanClick?: (span: Span) => void;
}) {
  const truncated = text.length > MAX_RENDER_CHARS;
  const shown = truncated ? text.slice(0, MAX_RENDER_CHARS) : text;
  const pieces = useMemo(() => buildPieces(shown, spans), [shown, spans]);

  return (
    <div className="whitespace-pre-wrap break-words font-mono text-xs leading-6">
      {pieces.map((piece) => {
        if (!piece.span) return <React.Fragment key={piece.key}>{piece.text}</React.Fragment>;
        const dimmed = Boolean(activeCode) && piece.span.code !== activeCode;
        const invisible = INVISIBLE_CODES.has(piece.span.code);
        return (
          <mark
            key={piece.key}
            title={`${piece.span.label} (${piece.span.code})`}
            onClick={() => onSpanClick?.(piece.span!)}
            className={cn(
              "cursor-pointer rounded-sm px-0.5",
              SPAN_STYLE[piece.span.severity] || SPAN_STYLE.warning,
              dimmed && "opacity-30"
            )}
          >
            {invisible ? "·".repeat(Math.max(1, piece.text.length)) : piece.text}
          </mark>
        );
      })}
      {truncated && (
        <div className="mt-2 text-[11px] text-muted-foreground">
          …đã cắt bớt, chương dài {text.length.toLocaleString("vi-VN")} ký tự.
        </div>
      )}
    </div>
  );
}
