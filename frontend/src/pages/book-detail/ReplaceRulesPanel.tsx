import React, { useCallback, useEffect, useState } from "react";
import { Check, Pencil, Plus, Replace, Trash2, X } from "lucide-react";
import { api } from "@/api";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { ReplaceRule, ReplaceRuleDeleteResult, ReplaceRuleResult, errorText } from "./types";
import { checkboxClass, fieldClass } from "./parts";

type Draft = { find: string; replace: string; is_regex: boolean; position: string };

const EMPTY_DRAFT: Draft = { find: "", replace: "", is_regex: false, position: "0" };

function draftForm(draft: Draft) {
  const form = new FormData();
  form.append("find", draft.find);
  form.append("replace", draft.replace);
  form.append("is_regex", draft.is_regex ? "true" : "false");
  form.append("position", String(Number(draft.position) || 0));
  return form;
}

/** Backend trả 303 cho form post kiểu cũ — SPA phải xin JSON mới nhận được luật vừa lưu. */
const postRule = <T,>(url: string, form?: FormData) =>
  api<T>(url, { method: "POST", body: form, headers: { Accept: "application/json" } });

function resetNote(count: number) {
  return count ? ` ${count} patch đã hoàn thành được đặt lại để TTS chạy lại.` : "";
}

/**
 * Luật tìm/thay cho cả sách (endpoint /books/{id}/replace-rules). Chạy sau bước
 * chuẩn hóa TTS, theo thứ tự "position" rồi tới id — luật sau nhìn thấy kết quả
 * của luật trước.
 */
export function ReplaceRulesPanel({ bookId, onMessage }: { bookId: string; onMessage: (message: string) => void }) {
  const [rules, setRules] = useState<ReplaceRule[]>();
  const [draft, setDraft] = useState<Draft>(EMPTY_DRAFT);
  const [editingId, setEditingId] = useState<number>();
  const [editDraft, setEditDraft] = useState<Draft>(EMPTY_DRAFT);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setRules(await api<ReplaceRule[]>(`/books/${bookId}/replace-rules`));
    } catch (error) {
      onMessage(errorText(error));
    }
  }, [bookId, onMessage]);

  useEffect(() => {
    load();
  }, [load]);

  const create = async () => {
    if (!draft.find.trim()) {
      onMessage("Cần nhập chuỗi cần tìm.");
      return;
    }
    setBusy(true);
    try {
      const result = await postRule<ReplaceRuleResult>(`/books/${bookId}/replace-rules`, draftForm(draft));
      setDraft(EMPTY_DRAFT);
      await load();
      onMessage(`Đã thêm luật "${result.rule.find}".${resetNote(result.reset_patches)}`);
    } catch (error) {
      onMessage(errorText(error));
    } finally {
      setBusy(false);
    }
  };

  const saveEdit = async (rule: ReplaceRule) => {
    if (!editDraft.find.trim()) {
      onMessage("Cần nhập chuỗi cần tìm.");
      return;
    }
    setBusy(true);
    try {
      const result = await postRule<ReplaceRuleResult>(
        `/books/${bookId}/replace-rules/${rule.id}/edit`,
        draftForm(editDraft)
      );
      setEditingId(undefined);
      await load();
      onMessage(`Đã cập nhật luật "${result.rule.find}".${resetNote(result.reset_patches)}`);
    } catch (error) {
      onMessage(errorText(error));
    } finally {
      setBusy(false);
    }
  };

  const remove = async (rule: ReplaceRule) => {
    if (!window.confirm(`Xoá luật thay thế "${rule.find}"?`)) return;
    setBusy(true);
    try {
      const result = await postRule<ReplaceRuleDeleteResult>(`/books/${bookId}/replace-rules/${rule.id}/delete`);
      await load();
      onMessage(`Đã xoá luật "${rule.find}".${resetNote(result.reset_patches)}`);
    } catch (error) {
      onMessage(errorText(error));
    } finally {
      setBusy(false);
    }
  };

  const startEdit = (rule: ReplaceRule) => {
    setEditingId(rule.id);
    setEditDraft({
      find: rule.find,
      replace: rule.replace,
      is_regex: rule.is_regex,
      position: String(rule.position),
    });
  };

  const inputClass = cn(fieldClass, "h-8 font-mono text-[11px]");

  return (
    <div className="space-y-3 rounded-md border border-border p-3">
      <div className="flex items-center gap-2">
        <Replace className="h-3.5 w-3.5 text-primary" />
        <span className="text-xs font-semibold">Luật tìm & thay của sách</span>
        <span className="text-[11px] text-muted-foreground">
          Áp sau khi chuẩn hóa, theo thứ tự bên dưới.
        </span>
      </div>

      {rules === undefined ? (
        <div className="py-3 text-center text-[11px] text-muted-foreground">Đang tải luật thay thế...</div>
      ) : rules.length === 0 ? (
        <div className="rounded-md border border-dashed border-border py-3 text-center text-[11px] text-muted-foreground">
          Chưa có luật nào. Thêm luật để thay chuỗi cố định (hoặc regex) trước khi TTS đọc.
        </div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-[11px]">
            <thead>
              <tr className="border-b border-border text-left text-muted-foreground">
                <th className="py-1.5 pr-2 font-medium">Tìm</th>
                <th className="py-1.5 pr-2 font-medium">Thay bằng</th>
                <th className="w-14 py-1.5 pr-2 font-medium">Regex</th>
                <th className="w-16 py-1.5 pr-2 font-medium">Thứ tự</th>
                <th className="w-20 py-1.5" />
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {rules.map((rule) =>
                editingId === rule.id ? (
                  <tr key={rule.id} className="bg-primary/5">
                    <td className="py-1.5 pr-2">
                      <input
                        className={inputClass}
                        value={editDraft.find}
                        onChange={(event) => setEditDraft({ ...editDraft, find: event.target.value })}
                        aria-label="Chuỗi cần tìm"
                      />
                    </td>
                    <td className="py-1.5 pr-2">
                      <input
                        className={inputClass}
                        value={editDraft.replace}
                        onChange={(event) => setEditDraft({ ...editDraft, replace: event.target.value })}
                        aria-label="Chuỗi thay thế"
                      />
                    </td>
                    <td className="py-1.5 pr-2">
                      <input
                        type="checkbox"
                        className={checkboxClass}
                        checked={editDraft.is_regex}
                        onChange={(event) => setEditDraft({ ...editDraft, is_regex: event.target.checked })}
                        aria-label="Dùng regex"
                      />
                    </td>
                    <td className="py-1.5 pr-2">
                      <input
                        type="number"
                        className={inputClass}
                        value={editDraft.position}
                        onChange={(event) => setEditDraft({ ...editDraft, position: event.target.value })}
                        aria-label="Thứ tự áp dụng"
                      />
                    </td>
                    <td className="py-1.5 text-right">
                      <Button size="sm" variant="ghost" className="h-7 px-2" disabled={busy} onClick={() => saveEdit(rule)} title="Lưu luật">
                        <Check className="h-3 w-3 text-emerald-600" />
                      </Button>
                      <Button size="sm" variant="ghost" className="h-7 px-2" disabled={busy} onClick={() => setEditingId(undefined)} title="Huỷ">
                        <X className="h-3 w-3" />
                      </Button>
                    </td>
                  </tr>
                ) : (
                  <tr key={rule.id}>
                    <td className="py-1.5 pr-2 font-mono break-all">{rule.find}</td>
                    <td className="py-1.5 pr-2 font-mono break-all text-muted-foreground">
                      {rule.replace || <span className="italic">(xoá chuỗi)</span>}
                    </td>
                    <td className="py-1.5 pr-2">
                      {rule.is_regex ? (
                        <span className="rounded bg-sky-100 px-1.5 py-0.5 text-[10px] font-medium text-sky-800">regex</span>
                      ) : (
                        <span className="text-muted-foreground">—</span>
                      )}
                    </td>
                    <td className="py-1.5 pr-2 font-mono">{rule.position}</td>
                    <td className="py-1.5 text-right">
                      <Button size="sm" variant="ghost" className="h-7 px-2" disabled={busy} onClick={() => startEdit(rule)} title="Sửa luật">
                        <Pencil className="h-3 w-3" />
                      </Button>
                      <Button size="sm" variant="ghost" className="h-7 px-2 text-red-600" disabled={busy} onClick={() => remove(rule)} title="Xoá luật">
                        <Trash2 className="h-3 w-3" />
                      </Button>
                    </td>
                  </tr>
                )
              )}
            </tbody>
          </table>
        </div>
      )}

      <div className="grid grid-cols-1 gap-2 sm:grid-cols-[1fr_1fr_auto_auto_auto] sm:items-center">
        <input
          className={inputClass}
          placeholder="Tìm..."
          value={draft.find}
          onChange={(event) => setDraft({ ...draft, find: event.target.value })}
          onKeyDown={(event) => event.key === "Enter" && create()}
          aria-label="Chuỗi cần tìm"
        />
        <input
          className={inputClass}
          placeholder="Thay bằng..."
          value={draft.replace}
          onChange={(event) => setDraft({ ...draft, replace: event.target.value })}
          onKeyDown={(event) => event.key === "Enter" && create()}
          aria-label="Chuỗi thay thế"
        />
        <label className="flex items-center gap-1.5 text-[11px] font-medium">
          <input
            type="checkbox"
            className={checkboxClass}
            checked={draft.is_regex}
            onChange={(event) => setDraft({ ...draft, is_regex: event.target.checked })}
          />
          Regex
        </label>
        <input
          type="number"
          className={cn(inputClass, "w-16")}
          value={draft.position}
          onChange={(event) => setDraft({ ...draft, position: event.target.value })}
          aria-label="Thứ tự áp dụng"
          title="Thứ tự áp dụng — số nhỏ chạy trước"
        />
        <Button size="sm" variant="outline" disabled={busy} onClick={create}>
          <Plus className="h-3.5 w-3.5" /> Thêm luật
        </Button>
      </div>

      <p className="text-[11px] text-muted-foreground">
        Thêm, sửa hoặc xoá luật đều reset các patch audio đã hoàn thành để TTS đọc lại theo luật mới.
      </p>
    </div>
  );
}
