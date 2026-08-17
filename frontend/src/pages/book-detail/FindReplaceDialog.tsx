import React, { useCallback, useEffect, useMemo, useState } from "react";
import { RotateCcw, Save, Search } from "lucide-react";
import { api, Patch, post, postJson, put } from "@/api";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/lib/utils";
import { PatchTextPayload, errorText } from "./types";
import { checkboxClass, fieldClass } from "./parts";

type Match = { start: number; length: number };

const CONTEXT_CHARS = 45;
const MAX_CONTEXTS = 50;

/**
 * Đếm khớp phía trình duyệt để xem trước. Regex của JS không giống hệt Python nên
 * đây chỉ là ước lượng — việc thay thế thật vẫn do server làm.
 * Trả về null khi regex không hợp lệ.
 */
function findMatches(text: string, query: string, isRegex: boolean): Match[] | null {
  if (!query) return [];
  let regex: RegExp;
  try {
    regex = new RegExp(isRegex ? query : query.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "g");
  } catch {
    return null;
  }
  const matches: Match[] = [];
  for (const match of text.matchAll(regex)) {
    matches.push({ start: match.index ?? 0, length: match[0].length });
    // Regex khớp rỗng (vd "a*") sẽ chạy vô hạn nếu không chặn — dừng ở đây là đủ.
    if (!match[0].length || matches.length >= 5000) break;
  }
  return matches;
}

export function FindReplaceDialog({
  bookId,
  patch,
  open,
  onOpenChange,
  onMessage,
  onSaved,
}: {
  bookId: string;
  patch?: Patch;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onMessage: (message: string) => void;
  onSaved: () => Promise<void> | void;
}) {
  const [text, setText] = useState("");
  const [savedText, setSavedText] = useState("");
  const [isEdited, setIsEdited] = useState(false);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [search, setSearch] = useState("");
  const [replacement, setReplacement] = useState("");
  const [isRegex, setIsRegex] = useState(false);

  const patchId = patch?.id;
  const base = `/books/${bookId}/text-studio/patches/${patchId}`;

  const load = useCallback(async () => {
    if (patchId === undefined) return;
    setLoading(true);
    try {
      const data = await api<PatchTextPayload>(`/books/${bookId}/text-studio/patches/${patchId}`);
      setText(data.text);
      setSavedText(data.text);
      setIsEdited(data.is_edited);
    } catch (error) {
      onMessage(errorText(error));
    } finally {
      setLoading(false);
    }
  }, [bookId, patchId, onMessage]);

  useEffect(() => {
    if (!open) return;
    setSearch("");
    setReplacement("");
    setIsRegex(false);
    load();
  }, [open, load]);

  const dirty = text !== savedText;
  const matches = useMemo(() => findMatches(text, search, isRegex), [text, search, isRegex]);
  const invalidRegex = matches === null;
  const contexts = useMemo(
    () =>
      (matches || []).slice(0, MAX_CONTEXTS).map((match) => ({
        before: text.slice(Math.max(0, match.start - CONTEXT_CHARS), match.start),
        hit: text.slice(match.start, match.start + match.length),
        after: text.slice(match.start + match.length, match.start + match.length + CONTEXT_CHARS),
        key: `${match.start}-${match.length}`,
      })),
    [matches, text]
  );

  const saveText = useCallback(async () => {
    const result = await put<{ ok: boolean; chunk_count: number }>(base, { text });
    setSavedText(text);
    setIsEdited(true);
    return result;
  }, [base, text]);

  const save = async () => {
    setBusy(true);
    try {
      const result = await saveText();
      onMessage(`Đã lưu text patch ${patch!.patch_index + 1} · ${result.chunk_count} chunk sau khi tách lại.`);
      await onSaved();
    } catch (error) {
      onMessage(errorText(error));
    } finally {
      setBusy(false);
    }
  };

  const replaceAll = async () => {
    if (!search) {
      onMessage("Cần nhập chuỗi cần tìm.");
      return;
    }
    setBusy(true);
    try {
      // Server thay trên text đang lưu, nên phải đẩy sửa tay lên trước kẻo bị mất.
      if (dirty) await saveText();
      const result = await postJson<{ text: string; replacements: number }>(`${base}/replace`, {
        search,
        replace: replacement,
        is_regex: isRegex,
      });
      setText(result.text);
      setSavedText(result.text);
      setIsEdited(true);
      onMessage(
        result.replacements
          ? `Đã thay ${result.replacements} chỗ trong patch ${patch!.patch_index + 1}.`
          : "Không tìm thấy chuỗi nào để thay."
      );
      await onSaved();
    } catch (error) {
      onMessage(errorText(error));
    } finally {
      setBusy(false);
    }
  };

  const resetText = async () => {
    if (!window.confirm("Bỏ mọi chỉnh sửa thủ công và dựng lại text từ nội dung chương gốc?")) return;
    setBusy(true);
    try {
      const result = (await post(`${base}/reset`)) as { text: string };
      setText(result.text);
      setSavedText(result.text);
      setIsEdited(false);
      onMessage(`Đã khôi phục text gốc cho patch ${patch!.patch_index + 1}.`);
      await onSaved();
    } catch (error) {
      onMessage(errorText(error));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="flex max-h-[90vh] max-w-3xl flex-col overflow-hidden">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2 pr-8">
            <span className="truncate">
              Tìm & thay · Patch #{patch ? patch.patch_index + 1 : ""} {patch?.name ? `· ${patch.name}` : ""}
            </span>
            {isEdited && (
              <span className="rounded bg-amber-100 px-1.5 py-0.5 text-[10px] font-medium text-amber-800">
                đã sửa tay
              </span>
            )}
          </DialogTitle>
          <DialogDescription>
            Thay thế chạy trên text của riêng patch này và ghi đè bản đã lưu. Muốn áp cho cả sách thì dùng luật
            tìm & thay trong Cấu hình sản xuất → Chuẩn hóa TTS.
          </DialogDescription>
        </DialogHeader>

        <div className="grid grid-cols-1 gap-2 sm:grid-cols-[1fr_1fr_auto_auto] sm:items-center">
          <input
            className={cn(fieldClass, "font-mono text-xs")}
            placeholder="Tìm..."
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            aria-label="Chuỗi cần tìm"
          />
          <input
            className={cn(fieldClass, "font-mono text-xs")}
            placeholder="Thay bằng..."
            value={replacement}
            onChange={(event) => setReplacement(event.target.value)}
            aria-label="Chuỗi thay thế"
          />
          <label className="flex items-center gap-1.5 text-xs font-medium">
            <input
              type="checkbox"
              className={checkboxClass}
              checked={isRegex}
              onChange={(event) => setIsRegex(event.target.checked)}
            />
            Regex
          </label>
          <Button size="sm" disabled={busy || loading || !search || invalidRegex} onClick={replaceAll}>
            <Search className="h-3.5 w-3.5" /> Thay tất cả
          </Button>
        </div>

        <div className="text-[11px] text-muted-foreground">
          {invalidRegex ? (
            <span className="text-red-600">Regex không hợp lệ.</span>
          ) : search ? (
            <>
              Ước lượng {matches!.length} khớp (phân biệt hoa/thường){matches!.length > MAX_CONTEXTS ? ` · hiện ${MAX_CONTEXTS} khớp đầu` : ""}.
            </>
          ) : (
            <>{text.length.toLocaleString("vi-VN")} ký tự{dirty ? " · có thay đổi chưa lưu" : ""}</>
          )}
        </div>

        <div className="min-h-0 flex-1 space-y-3 overflow-auto">
          {search && !invalidRegex && contexts.length > 0 && (
            <div className="divide-y divide-border rounded-md border border-border">
              {contexts.map((context) => (
                <div key={context.key} className="px-3 py-1.5 font-mono text-[11px] leading-5">
                  <span className="text-muted-foreground">…{context.before}</span>
                  <mark className="rounded-sm bg-amber-200 px-0.5 text-amber-900">{context.hit}</mark>
                  <span className="text-muted-foreground">{context.after}…</span>
                </div>
              ))}
            </div>
          )}

          <Textarea
            className="min-h-64 font-mono text-[11px] leading-5"
            value={loading ? "Đang tải text của patch..." : text}
            onChange={(event) => setText(event.target.value)}
            readOnly={loading}
            aria-label="Text của patch"
          />
        </div>

        <DialogFooter className="gap-2">
          <Button variant="outline" size="sm" disabled={busy || loading} onClick={resetText}>
            <RotateCcw className="h-3.5 w-3.5" /> Khôi phục text gốc
          </Button>
          <Button size="sm" disabled={busy || loading || !dirty} onClick={save}>
            <Save className="h-3.5 w-3.5" /> {busy ? "Đang lưu..." : "Lưu text"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
