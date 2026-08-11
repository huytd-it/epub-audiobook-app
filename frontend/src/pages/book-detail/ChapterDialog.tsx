import React, { useEffect, useMemo, useState } from "react";
import { AlertTriangle, ChevronLeft, ChevronRight, RefreshCw, Wand2 } from "lucide-react";
import { api, put } from "@/api";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { cn } from "@/lib/utils";
import { ChapterAnalyzeResult, ChapterDetail, ChapterSaveResult, Span, errorText } from "./types";
import { CheckField, SeverityTag, TabBar, fieldClass } from "./parts";
import { HighlightedText } from "./HighlightedText";

const TITLE_STATE_LABEL: Record<string, string> = {
  canonical: "Chuẩn",
  fixable: "Cần sửa",
  no_name: "Thiếu tên",
  unknown: "Không nhận diện được",
};

const SPAN_CHIP_STYLE: Record<string, string> = {
  error: "bg-red-100 text-red-800 hover:bg-red-200",
  warning: "bg-amber-100 text-amber-800 hover:bg-amber-200",
  info: "bg-sky-100 text-sky-800 hover:bg-sky-200",
};

export function ChapterDialog({
  bookId,
  chapterIndex,
  chapterCount,
  open,
  onOpenChange,
  onChapterIndexChange,
  onMessage,
  onSaved,
}: {
  bookId: string;
  chapterIndex?: number;
  chapterCount: number;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onChapterIndexChange: (index: number) => void;
  onMessage: (message: string) => void;
  onSaved: () => Promise<void> | void;
}) {
  const [detail, setDetail] = useState<ChapterDetail>();
  const [loading, setLoading] = useState(false);
  const [mode, setMode] = useState<"view" | "edit">("view");
  const [draftTitle, setDraftTitle] = useState("");
  const [draftText, setDraftText] = useState("");
  const [draftExcluded, setDraftExcluded] = useState(false);
  const [draftSpans, setDraftSpans] = useState<Span[] | null>(null);
  const [draftIssues, setDraftIssues] = useState<ChapterDetail["report"]["issues"]>();
  const [draftSeverity, setDraftSeverity] = useState<ChapterDetail["report"]["severity"]>();
  const [saving, setSaving] = useState(false);
  const [analyzing, setAnalyzing] = useState(false);
  const [activeCode, setActiveCode] = useState<string>();

  useEffect(() => {
    if (!open || chapterIndex === undefined) return;
    let cancelled = false;
    setDetail(undefined);
    setLoading(true);
    setMode("view");
    setDraftSpans(null);
    setActiveCode(undefined);
    api<ChapterDetail>(`/books/${bookId}/chapters/${chapterIndex}?analyze=1`)
      .then((data) => {
        if (cancelled) return;
        setDetail(data);
        setDraftTitle(data.title);
        setDraftText(data.text);
        setDraftExcluded(data.is_excluded);
      })
      .catch((error) => !cancelled && onMessage(errorText(error)))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [open, chapterIndex, bookId, onMessage]);

  const dirty = Boolean(
    detail && (draftTitle !== detail.title || draftText !== detail.text || draftExcluded !== detail.is_excluded)
  );

  const effectiveSpans = draftSpans ?? detail?.spans ?? [];
  const effectiveIssues = draftIssues ?? detail?.report.issues ?? [];
  const effectiveSeverity = draftSeverity ?? detail?.report.severity ?? "ok";
  const effectiveTotals = useMemo(() => {
    const totals: Record<string, number> = {};
    for (const span of effectiveSpans) totals[span.code] = (totals[span.code] || 0) + 1;
    return totals;
  }, [effectiveSpans]);

  const goTo = (index: number) => {
    if (dirty || index < 0 || index >= chapterCount) return;
    onChapterIndexChange(index);
  };

  const reanalyze = async () => {
    if (chapterIndex === undefined) return;
    setAnalyzing(true);
    try {
      const result = await api<ChapterAnalyzeResult>(`/books/${bookId}/chapters/${chapterIndex}/analyze`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title: draftTitle, text: draftText }),
      });
      setDraftSpans(result.spans);
      setDraftIssues(result.report.issues);
      setDraftSeverity(result.report.severity);
      setMode("view");
    } catch (error) {
      onMessage(errorText(error));
    } finally {
      setAnalyzing(false);
    }
  };

  const save = async () => {
    if (chapterIndex === undefined) return;
    setSaving(true);
    try {
      const result = await put<ChapterSaveResult>(`/books/${bookId}/chapters/${chapterIndex}`, {
        title: draftTitle,
        text: draftText,
        is_excluded: draftExcluded,
      });
      setDetail((current) =>
        current
          ? {
              ...current,
              title: result.title,
              text: result.text,
              char_count: result.char_count,
              is_excluded: result.is_excluded,
              chapter_no: result.chapter_no,
              title_state: result.title_state,
              suggested_title: result.suggested_title,
              report: result.report,
              spans: result.spans,
              span_totals: result.span_totals,
              patches: result.patches,
            }
          : current
      );
      setDraftSpans(null);
      setDraftIssues(undefined);
      setDraftSeverity(undefined);
      setMode("view");
      onMessage(
        `Đã lưu chương ${chapterIndex + 1}.` +
          (result.patches_recomputed.length
            ? ` Đã tính lại chunk cho ${result.patches_recomputed.length} patch.`
            : "")
      );
      await onSaved();
    } catch (error) {
      onMessage(errorText(error));
    } finally {
      setSaving(false);
    }
  };

  const useSuggestedTitle = () => {
    if (detail?.suggested_title) setDraftTitle(detail.suggested_title);
  };

  const doneWarning = detail?.patches.find((patch) => patch.status === "done");
  const cleanTextWarning = detail?.patches.find((patch) => patch.has_clean_text);
  const processingWarning = detail?.patches.find((patch) => patch.status === "processing");

  return (
    <Dialog open={open} onOpenChange={(next) => (!next && dirty ? undefined : onOpenChange(next))}>
      <DialogContent className="flex max-h-[90vh] max-w-5xl flex-col overflow-hidden p-0">
        <DialogHeader className="shrink-0 border-b border-border px-6 py-4">
          <div className="flex items-center justify-between gap-3">
            <DialogTitle className="flex min-w-0 items-center gap-2">
              <Button
                variant="ghost"
                size="icon"
                className="h-6 w-6 shrink-0"
                disabled={dirty || chapterIndex === undefined || chapterIndex <= 0}
                onClick={() => chapterIndex !== undefined && goTo(chapterIndex - 1)}
                title="Chương trước"
              >
                <ChevronLeft className="h-4 w-4" />
              </Button>
              <span className="shrink-0">Chương {chapterIndex !== undefined ? chapterIndex + 1 : ""}</span>
              <SeverityTag value={effectiveSeverity} />
              <Button
                variant="ghost"
                size="icon"
                className="h-6 w-6 shrink-0"
                disabled={dirty || chapterIndex === undefined || chapterIndex >= chapterCount - 1}
                onClick={() => chapterIndex !== undefined && goTo(chapterIndex + 1)}
                title="Chương sau"
              >
                <ChevronRight className="h-4 w-4" />
              </Button>
            </DialogTitle>
          </div>

          {detail && (
            <div className="mt-1">
              {mode === "edit" ? (
                <input
                  className={cn(fieldClass, "font-medium")}
                  value={draftTitle}
                  onChange={(event) => setDraftTitle(event.target.value)}
                  placeholder="Chương N: Tên chương"
                />
              ) : (
                <div className="truncate text-sm font-medium" title={detail.title}>
                  {detail.title || <span className="text-muted-foreground">(không có tiêu đề)</span>}
                </div>
              )}
              <div className="mt-2 flex flex-wrap items-center gap-1.5 text-[11px]">
                <span className="rounded bg-muted px-1.5 py-0.5 text-muted-foreground">
                  Số chương: {detail.chapter_no ?? "—"}
                </span>
                <span
                  className={cn(
                    "rounded px-1.5 py-0.5",
                    detail.title_state === "canonical" ? "bg-emerald-50 text-emerald-700" : "bg-amber-50 text-amber-800"
                  )}
                >
                  Định dạng: {TITLE_STATE_LABEL[detail.title_state] || detail.title_state}
                </span>
                {detail.suggested_title && mode === "edit" && draftTitle !== detail.suggested_title && (
                  <button
                    onClick={useSuggestedTitle}
                    className="inline-flex items-center gap-1 rounded bg-primary/10 px-1.5 py-0.5 text-primary hover:bg-primary/20"
                  >
                    <Wand2 className="h-3 w-3" /> Dùng đề xuất: {detail.suggested_title}
                  </button>
                )}
              </div>
            </div>
          )}
        </DialogHeader>

        {(doneWarning || cleanTextWarning || processingWarning) && (
          <div className="shrink-0 space-y-1 border-b border-border bg-amber-50 px-6 py-2 text-[11px] text-amber-900">
            {doneWarning && (
              <div className="flex items-start gap-1.5">
                <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" />
                <span>
                  Chương này nằm trong patch #{doneWarning.patch_index + 1} đã tạo audio. Sửa nội dung sẽ không tự
                  tạo lại audio — cần chạy lại TTS cho patch đó.
                </span>
              </div>
            )}
            {cleanTextWarning && (
              <div className="flex items-start gap-1.5">
                <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" />
                <span>
                  Patch #{cleanTextWarning.patch_index + 1} đang dùng bản sửa riêng trong Text Studio, nên sửa chương
                  sẽ không đổi giọng đọc của patch đó.
                </span>
              </div>
            )}
            {processingWarning && (
              <div className="flex items-start gap-1.5">
                <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" />
                <span>Patch #{processingWarning.patch_index + 1} đang tổng hợp; số chunk sẽ không được tính lại.</span>
              </div>
            )}
          </div>
        )}

        <div className="shrink-0 flex items-center justify-between gap-3 border-b border-border px-6 py-2">
          <TabBar<"view" | "edit">
            value={mode}
            onChange={setMode}
            tabs={[
              { value: "view", label: "Xem lỗi" },
              { value: "edit", label: "Chỉnh sửa" },
            ]}
          />
          <div className="hidden items-center gap-2 text-[10px] text-muted-foreground sm:flex">
            <span className="inline-flex items-center gap-1">
              <span className="h-2.5 w-2.5 rounded-sm bg-red-200 ring-1 ring-red-400" /> Lỗi
            </span>
            <span className="inline-flex items-center gap-1">
              <span className="h-2.5 w-2.5 rounded-sm bg-amber-200" /> Cảnh báo
            </span>
            <span className="inline-flex items-center gap-1">
              <span className="h-2.5 w-2.5 rounded-sm bg-sky-100" /> Ghi chú
            </span>
          </div>
        </div>

        {loading || !detail ? (
          <div className="flex-1 py-12 text-center text-xs text-muted-foreground">Đang tải chương...</div>
        ) : (
          <div className="grid flex-1 gap-4 overflow-hidden px-6 py-4 lg:grid-cols-[1fr_16rem]">
            <div className="max-h-[52vh] overflow-auto rounded-md border border-border p-3">
              {mode === "view" ? (
                <HighlightedText
                  text={detail.text}
                  spans={effectiveSpans}
                  activeCode={activeCode}
                  onSpanClick={(span) => setActiveCode((current) => (current === span.code ? undefined : span.code))}
                />
              ) : (
                <Textarea
                  className="min-h-[380px] border-0 p-0 font-mono text-xs shadow-none focus-visible:ring-0"
                  value={draftText}
                  onChange={(event) => setDraftText(event.target.value)}
                />
              )}
            </div>

            <div className="max-h-[52vh] space-y-3 overflow-auto text-xs">
              {Object.keys(effectiveTotals).length > 0 && (
                <div>
                  <div className="mb-1.5 font-medium">Lỗi phát hiện</div>
                  <div className="flex flex-wrap gap-1">
                    {Object.entries(effectiveTotals).map(([code, count]) => {
                      const span = effectiveSpans.find((item) => item.code === code);
                      return (
                        <button
                          key={code}
                          onClick={() => setActiveCode((current) => (current === code ? undefined : code))}
                          className={cn(
                            "rounded px-1.5 py-0.5 text-[10px] font-medium",
                            SPAN_CHIP_STYLE[span?.severity || "warning"],
                            activeCode === code && "ring-2 ring-primary/40"
                          )}
                          title={span?.label}
                        >
                          {span?.label || code} · {count}
                        </button>
                      );
                    })}
                  </div>
                </div>
              )}

              {effectiveIssues.length > 0 && (
                <div>
                  <div className="mb-1.5 font-medium">Chi tiết</div>
                  <ul className="space-y-1.5">
                    {effectiveIssues.map((issue, index) => (
                      <li key={`${issue.code}-${index}`} className="flex items-start gap-1.5 text-muted-foreground">
                        <SeverityTag value={issue.severity} />
                        <span>{issue.message}</span>
                      </li>
                    ))}
                  </ul>
                </div>
              )}

              {effectiveIssues.length === 0 && Object.keys(effectiveTotals).length === 0 && (
                <div className="text-muted-foreground">Không phát hiện lỗi trong chương này.</div>
              )}
            </div>
          </div>
        )}

        <DialogFooter className="shrink-0 flex-row items-center justify-between border-t border-border px-6 py-3 sm:justify-between">
          <CheckField checked={draftExcluded} onChange={setDraftExcluded} label="Bỏ qua chương này khi tạo audio" />
          <div className="flex items-center gap-2">
            <Button variant="outline" size="sm" onClick={reanalyze} disabled={analyzing || loading}>
              <RefreshCw className={cn("h-3.5 w-3.5", analyzing && "animate-spin")} /> Phân tích lại
            </Button>
            <Button variant="ghost" size="sm" onClick={() => onOpenChange(false)}>
              Huỷ
            </Button>
            <Button size="sm" onClick={save} disabled={!dirty || saving}>
              {saving ? "Đang lưu..." : "Lưu thay đổi"}
            </Button>
          </div>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
