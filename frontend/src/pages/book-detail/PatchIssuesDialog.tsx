import React, { useEffect, useState } from "react";
import { AlertTriangle, CheckCircle2, ScanText, Wrench } from "lucide-react";
import { api, Chapter } from "@/api";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { cn } from "@/lib/utils";
import { PatchRangeReport, PatchTextCheck, TextWarning, errorText } from "./types";
import { SeverityTag, TabBar } from "./parts";

const KIND_LABEL: Record<string, string> = {
  junk: "Ký tự rác",
  abbreviation: "Từ viết tắt",
  spell_vi: "Nghi sai chính tả",
  effect_marker: "Đánh dấu hiệu ứng",
  sound_desc: "Mô tả âm thanh",
};

const KIND_STYLE: Record<string, string> = {
  junk: "bg-red-100 text-red-800 hover:bg-red-200",
  abbreviation: "bg-amber-100 text-amber-800 hover:bg-amber-200",
  spell_vi: "bg-sky-100 text-sky-800 hover:bg-sky-200",
  effect_marker: "bg-muted text-muted-foreground hover:bg-muted/70",
  sound_desc: "bg-muted text-muted-foreground hover:bg-muted/70",
};

/** Đoạn văn quanh lỗi, phần lỗi được tô đậm. */
function WarningContext({ warning }: { warning: TextWarning }) {
  const before = warning.context.slice(0, warning.context_offset);
  const hit = warning.context.slice(warning.context_offset, warning.context_offset + warning.length);
  const after = warning.context.slice(warning.context_offset + warning.length);
  return (
    <span className="font-mono text-[11px] leading-5">
      <span className="text-muted-foreground">…{before}</span>
      <mark className={cn("rounded-sm px-0.5", KIND_STYLE[warning.kind] || "bg-amber-100 text-amber-900")}>{hit}</mark>
      <span className="text-muted-foreground">{after}…</span>
    </span>
  );
}

export function PatchIssuesDialog({
  bookId,
  patchId,
  rangeReport,
  chapters,
  open,
  onOpenChange,
  onMessage,
}: {
  bookId: string;
  patchId?: number;
  rangeReport?: PatchRangeReport;
  chapters: Chapter[];
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onMessage: (message: string) => void;
}) {
  const [tab, setTab] = useState<"range" | "text">("range");
  const [check, setCheck] = useState<PatchTextCheck>();
  const [loading, setLoading] = useState(false);
  const [activeKind, setActiveKind] = useState<string>();

  useEffect(() => {
    if (!open) return;
    setTab(rangeReport && rangeReport.severity !== "ok" ? "range" : "text");
    setCheck(undefined);
    setActiveKind(undefined);
  }, [open, patchId, rangeReport]);

  // Soát chữ tốn CPU (chuẩn hoá lại toàn bộ text của patch) nên chỉ chạy khi mở tab đó.
  useEffect(() => {
    if (!open || tab !== "text" || patchId === undefined || check) return;
    let cancelled = false;
    setLoading(true);
    api<PatchTextCheck>(`/books/${bookId}/patches/${patchId}/text-check`)
      .then((data) => !cancelled && setCheck(data))
      .catch((error) => !cancelled && onMessage(errorText(error)))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [open, tab, patchId, bookId, check, onMessage]);

  const visibleItems = check?.items.filter((item) => !activeKind || item.kind === activeKind) || [];

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="flex max-h-[90vh] max-w-3xl flex-col overflow-hidden">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2 pr-8">
            <span className="truncate">
              Patch #{rangeReport ? rangeReport.patch_index + 1 : ""} · {rangeReport?.name}
            </span>
            {rangeReport && <SeverityTag value={rangeReport.severity} />}
          </DialogTitle>
          <DialogDescription>
            {rangeReport
              ? `${rangeReport.chapter_count} chương thực tế trong patch` +
                (rangeReport.actual_no_start != null
                  ? ` · chương ${rangeReport.actual_no_start}–${rangeReport.actual_no_end}`
                  : " · chưa đọc được số chương")
              : "—"}
          </DialogDescription>
        </DialogHeader>

        <TabBar<"range" | "text">
          value={tab}
          onChange={setTab}
          tabs={[
            { value: "range", label: "Khoảng chương", badge: rangeReport?.issues.length || 0 },
            { value: "text", label: "Lỗi chữ / TTS", badge: check?.total },
          ]}
        />

        <div className="min-h-0 flex-1 overflow-auto">
          {tab === "range" && (
            <div className="space-y-3 py-2 text-xs">
              {!rangeReport || rangeReport.issues.length === 0 ? (
                <div className="flex items-center gap-2 rounded-md bg-emerald-50 px-3 py-2 text-emerald-700">
                  <CheckCircle2 className="h-3.5 w-3.5" /> Khoảng chương của patch này hợp lệ.
                </div>
              ) : (
                <ul className="space-y-2">
                  {rangeReport.issues.map((issue, index) => (
                    <li
                      key={`${issue.code}-${index}`}
                      className={cn(
                        "flex items-start gap-2 rounded-md px-3 py-2",
                        issue.severity === "error" ? "bg-red-50 text-red-800" : "bg-amber-50 text-amber-900"
                      )}
                    >
                      <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                      <div className="min-w-0">
                        <div className="font-mono text-[10px] opacity-70">{issue.code}</div>
                        <div>{issue.message}</div>
                      </div>
                    </li>
                  ))}
                </ul>
              )}

              {rangeReport && (
                <div className="grid grid-cols-2 gap-2 rounded-md border border-border p-3 text-[11px]">
                  <div>
                    <div className="text-muted-foreground">Chỉ số chương</div>
                    <div className="font-mono">
                      {rangeReport.chapter_start + 1}–{rangeReport.chapter_end + 1}
                    </div>
                  </div>
                  <div>
                    <div className="text-muted-foreground">Số chương đã neo</div>
                    <div className="font-mono">
                      {rangeReport.stored_no_start ?? "—"}–{rangeReport.stored_no_end ?? "—"}
                    </div>
                  </div>
                  <div>
                    <div className="text-muted-foreground">Số chương thực tế</div>
                    <div
                      className={cn(
                        "font-mono",
                        rangeReport.stored_no_start != null &&
                          rangeReport.actual_no_start !== rangeReport.stored_no_start &&
                          "font-bold text-red-600"
                      )}
                    >
                      {rangeReport.actual_no_start ?? "—"}–{rangeReport.actual_no_end ?? "—"}
                    </div>
                  </div>
                  <div>
                    <div className="text-muted-foreground">Chương không có số</div>
                    <div className="font-mono">{rangeReport.unnumbered_count}</div>
                  </div>
                </div>
              )}

              <div>
                <div className="mb-2 font-semibold">Danh sách chương trong patch</div>
                {chapters.length ? (
                  <ol className="divide-y divide-border rounded-md border border-border">
                    {chapters.map((chapter) => (
                      <li key={chapter.id} className="flex items-start gap-3 px-3 py-2">
                        <span className="w-9 shrink-0 font-mono text-[10px] text-muted-foreground">
                          {chapter.chapter_no != null ? `#${chapter.chapter_no}` : `M${chapter.chapter_index + 1}`}
                        </span>
                        <span className="min-w-0 break-words">{chapter.title || "Chương không có tiêu đề"}</span>
                      </li>
                    ))}
                  </ol>
                ) : (
                  <div className="rounded-md border border-dashed border-border px-3 py-4 text-center text-muted-foreground">
                    Không có chương nào trong khoảng này.
                  </div>
                )}
              </div>
            </div>
          )}

          {tab === "text" && (
            <div className="space-y-3 py-2 text-xs">
              {loading ? (
                <div className="py-8 text-center text-muted-foreground">
                  <ScanText className="mx-auto mb-2 h-4 w-4 animate-pulse" />
                  Đang soát chính tả, ký tự rác và từ viết tắt...
                </div>
              ) : !check ? (
                <div className="py-8 text-center text-muted-foreground">Chưa có dữ liệu.</div>
              ) : check.total === 0 ? (
                <div className="flex items-center gap-2 rounded-md bg-emerald-50 px-3 py-2 text-emerald-700">
                  <CheckCircle2 className="h-3.5 w-3.5" /> Không phát hiện lỗi chữ nào ảnh hưởng TTS.
                </div>
              ) : (
                <>
                  <div className="flex flex-wrap items-center gap-1.5">
                    <span className="text-muted-foreground">
                      {check.total} lỗi trên {check.chars.toLocaleString("vi-VN")} ký tự:
                    </span>
                    {Object.entries(check.totals).map(([kind, count]) => (
                      <button
                        key={kind}
                        onClick={() => setActiveKind((current) => (current === kind ? undefined : kind))}
                        className={cn(
                          "rounded px-1.5 py-0.5 text-[10px] font-medium",
                          KIND_STYLE[kind] || "bg-muted text-muted-foreground",
                          activeKind === kind && "ring-2 ring-primary/40"
                        )}
                      >
                        {KIND_LABEL[kind] || kind} · {count}
                      </button>
                    ))}
                  </div>

                  <div className="divide-y divide-border rounded-md border border-border">
                    {visibleItems.slice(0, 200).map((item, index) => (
                      <div key={`${item.kind}-${item.position}-${index}`} className="px-3 py-2">
                        <div className="mb-1 flex items-center gap-2">
                          <span
                            className={cn(
                              "rounded px-1.5 py-0.5 text-[10px] font-medium",
                              KIND_STYLE[item.kind] || "bg-muted text-muted-foreground"
                            )}
                          >
                            {KIND_LABEL[item.kind] || item.kind}
                          </span>
                          <span className="font-mono text-[11px] font-semibold">{item.original}</span>
                          {item.suggestion && (
                            <span className="inline-flex items-center gap-1 text-[10px] text-emerald-700">
                              <Wrench className="h-3 w-3" /> đọc là "{item.suggestion}"
                            </span>
                          )}
                        </div>
                        <WarningContext warning={item} />
                      </div>
                    ))}
                  </div>

                  {visibleItems.length > 200 && (
                    <div className="text-[11px] text-muted-foreground">
                      Đang hiện 200 lỗi đầu trên tổng {visibleItems.length}.
                    </div>
                  )}
                </>
              )}
            </div>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}
