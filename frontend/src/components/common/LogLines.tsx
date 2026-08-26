import React, { useMemo, useState } from "react";
import { ChevronRight } from "lucide-react";

interface ParsedLine {
  raw: string;
  timestamp?: string;
  level?: string;
  phase?: string;
  message: string;
}

// "2026-08-26T09:23:33Z [INFO ] phase=synthesizing | message" -- app/jobqueue/joblog.py
const JOB_LOG_RE = /^(\S+)\s+\[(\w+)\s*\]\s+(?:phase=(\S*)\s*\|\s*)?(.*)$/;
// "2026-08-26 09:23:33,123 [INFO] app.tts_engine: message" -- app/main.py logging.basicConfig
const APP_LOG_RE = /^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})\s+\[(\w+)\]\s+[\w.]+:\s+(.*)$/;

function parseLine(raw: string): ParsedLine {
  if (raw.startsWith("@@EVENT ")) {
    return { raw, level: "EVENT", message: raw.slice("@@EVENT ".length) };
  }
  let m = JOB_LOG_RE.exec(raw);
  if (m) return { raw, timestamp: m[1], level: m[2].trim(), phase: m[3] || undefined, message: m[4] };
  m = APP_LOG_RE.exec(raw);
  if (m) return { raw, timestamp: m[1], level: m[2], message: m[3] };
  return { raw, message: raw };
}

const LEVEL_STYLES: Record<string, string> = {
  ERROR: "text-red-400 border-red-900/60 bg-red-950/40",
  FATAL: "text-red-400 border-red-900/60 bg-red-950/40",
  WARN: "text-amber-400 border-amber-900/60 bg-amber-950/30",
  WARNING: "text-amber-400 border-amber-900/60 bg-amber-950/30",
  INFO: "text-slate-400 border-slate-800 bg-slate-900/40",
  DEBUG: "text-slate-500 border-slate-800 bg-slate-900/20",
  EVENT: "text-sky-400 border-sky-900/60 bg-sky-950/30",
};

const SUMMARY_LIMIT = 160;

/** Nhật ký thô hiển thị theo từng dòng: mỗi dòng một tóm tắt ngắn, click để xem
 * toàn bộ nội dung -- dùng chung cho modal log của job (Queue.tsx) và trang
 * "Nhật ký hệ thống" (LogsPage.tsx), vì dòng log chunk văn bản TTS có thể dài
 * hàng trăm ký tự và làm loãng cả khối <pre> nếu hiện hết cùng lúc. */
export function LogLines({ text, emptyText }: { text: string; emptyText: string }) {
  const parsedLines = useMemo(
    () => (text ? text.split("\n").filter((line) => line.length > 0).map(parseLine) : []),
    [text],
  );
  const [expanded, setExpanded] = useState<Set<number>>(new Set());

  if (parsedLines.length === 0) {
    return <p className="font-mono text-xs text-slate-500">{emptyText}</p>;
  }

  function toggle(i: number) {
    setExpanded((previous) => {
      const next = new Set(previous);
      if (next.has(i)) next.delete(i); else next.add(i);
      return next;
    });
  }

  return (
    <div className="flex flex-col gap-0.5 font-mono text-xs">
      {parsedLines.map((parsed, i) => {
        const isLong = parsed.message.length > SUMMARY_LIMIT;
        const isOpen = expanded.has(i);
        const summary = isLong && !isOpen ? `${parsed.message.slice(0, SUMMARY_LIMIT)}…` : parsed.message;
        const levelStyle = LEVEL_STYLES[parsed.level ?? ""] ?? "text-slate-400 border-slate-800 bg-slate-900/40";
        return (
          <div
            key={i}
            className={`group flex items-start gap-2 rounded px-2 py-1 ${isLong ? "cursor-pointer hover:bg-slate-800/60" : ""}`}
            onClick={isLong ? () => toggle(i) : undefined}
          >
            <ChevronRight
              className={`mt-0.5 h-3 w-3 shrink-0 text-slate-600 transition-transform ${isLong ? "" : "invisible"} ${isOpen ? "rotate-90" : ""}`}
            />
            {parsed.timestamp && (
              <span className="shrink-0 text-slate-600">{parsed.timestamp.replace("T", " ").replace("Z", "")}</span>
            )}
            {parsed.level && (
              <span className={`shrink-0 rounded border px-1.5 py-0 text-[10px] leading-4 ${levelStyle}`}>
                {parsed.level}
              </span>
            )}
            {parsed.phase && <span className="shrink-0 text-slate-600">phase={parsed.phase}</span>}
            <span className="whitespace-pre-wrap break-words text-slate-200">{summary}</span>
          </div>
        );
      })}
    </div>
  );
}
