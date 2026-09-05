import React, { useMemo, useState } from "react";
import { ChevronDown, Clipboard, Download, Gauge, HardDrive, KeyRound, Save } from "lucide-react";
import { api, DriveAccount, DriveTarget, KaggleAccount, Patch, post, postJson } from "@/api";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/lib/utils";
import { AudioSettings, TtsModel, VoiceOption, downloadForm, errorText } from "./types";
import { CheckField, Field, SectionHead, fieldClass, selectClass } from "./parts";

/**
 * Không giữ danh sách chọn riêng: patch để export lấy thẳng từ lựa chọn trong bảng Patches.
 * Không chọn gì = export mọi patch đang sẵn sàng.
 */
export function ExportPanel({
  bookId,
  patches,
  selectedIds,
  accounts,
  syncTargets,
  kaggleAccounts,
  settings,
  onSettingsChange,
  ttsModels,
  voiceOptions,
  onMessage,
  onRefresh,
  onBusyChange,
}: {
  bookId: string;
  patches: Patch[];
  selectedIds: number[];
  accounts: DriveAccount[];
  syncTargets: DriveTarget[];
  kaggleAccounts: KaggleAccount[];
  settings: AudioSettings;
  onSettingsChange: (patch: Partial<AudioSettings>) => void;
  ttsModels: TtsModel[];
  voiceOptions: VoiceOption[];
  onMessage: (message: string) => void;
  onRefresh: () => Promise<void> | void;
  onBusyChange: (busy: boolean) => void;
}) {
  const [syncTargetId, setSyncTargetId] = useState("");
  const [accountId, setAccountId] = useState("");
  const [exporting, setExporting] = useState(false);
  const [saving, setSaving] = useState(false);
  const [credentials, setCredentials] = useState("");
  const [credentialsOpen, setCredentialsOpen] = useState(false);

  const { targetIds, skipped, usingSelection } = useMemo(() => {
    const exportable = patches.filter((patch) => patch.status !== "processing");
    if (!selectedIds.length) {
      return { targetIds: exportable.map((patch) => patch.id), skipped: 0, usingSelection: false };
    }
    const chosen = exportable.filter((patch) => selectedIds.includes(patch.id));
    return {
      targetIds: chosen.map((patch) => patch.id),
      skipped: selectedIds.length - chosen.length,
      usingSelection: true,
    };
  }, [patches, selectedIds]);

  const buildForm = () => {
    const form = new FormData();
    targetIds.forEach((id) => form.append("patch_ids", String(id)));
    form.set("model_id", settings.modelId);
    form.set("voice_id", settings.voiceId);
    form.set("max_chars", settings.maxChars || "0");
    form.set("with_effects", settings.withEffects ? "1" : "0");
    return form;
  };

  const runExport = async (action: (form: FormData) => Promise<unknown>, done: string, refresh = true) => {
    if (!targetIds.length) {
      onMessage("Không có patch sẵn sàng để export.");
      return;
    }
    setExporting(true);
    onBusyChange(true);
    try {
      await action(buildForm());
      onMessage(done);
      if (refresh) await onRefresh();
    } catch (error) {
      onMessage(errorText(error));
    } finally {
      setExporting(false);
      onBusyChange(false);
    }
  };

  const saveSettings = async () => {
    setSaving(true);
    onBusyChange(true);
    try {
      await postJson(`/books/${bookId}/export-audio-settings`, {
        model_id: settings.modelId,
        voice_id: settings.voiceId,
        max_chars: settings.maxChars ? Number(settings.maxChars) : 1200,
        with_effects: settings.withEffects,
      });
      onMessage("Đã lưu cấu hình Export Colab / Kaggle.");
    } catch (error) {
      onMessage(errorText(error));
    } finally {
      setSaving(false);
      onBusyChange(false);
    }
  };

  const downloadZip = () =>
    runExport(
      (form) => downloadForm(`/books/${bookId}/patches/export-batch/download`, form),
      `Đã tải gói ZIP (${targetIds.length} patch).`,
      false
    );

  const exportToDesktop = () => {
    if (!syncTargetId) {
      onMessage("Chọn Drive Desktop target.");
      return;
    }
    return runExport((form) => {
      form.set("sync_target_id", syncTargetId);
      return post(`/books/${bookId}/patches/export-batch`, form);
    }, `Đã export ${targetIds.length} patch sang Drive Desktop.`);
  };

  const exportToDriveApi = () => {
    if (!accountId) {
      onMessage("Chọn Google Drive account.");
      return;
    }
    return runExport((form) => {
      form.set("account_id", accountId);
      return post(`/books/${bookId}/patches/export-batch-api`, form);
    }, `Đã export ${targetIds.length} patch qua Google Drive API.`);
  };

  const exportToKaggle = () =>
    runExport(
      (form) => post(`/books/${bookId}/patches/export-batch-kaggle`, form),
      `Đã đưa ${targetIds.length} patch vào hàng đợi Kaggle. Theo dõi tiến độ ở trang Queue.`
    );

  const loadCredentials = async () => {
    if (!accountId) {
      onMessage("Chọn Google Drive account trước khi lấy credentials.");
      return;
    }
    try {
      const payload = await api<unknown>(`/drive/kaggle-credentials?account_id=${accountId}`);
      setCredentials(JSON.stringify(payload, null, 2));
    } catch (error) {
      onMessage(errorText(error));
    }
  };

  const copyCredentials = async () => {
    try {
      await navigator.clipboard.writeText(credentials);
      onMessage("Đã copy GDRIVE_CREDS.");
    } catch {
      onMessage("Trình duyệt chặn clipboard, hãy copy thủ công.");
    }
  };

  return (
    <Card>
      <CardHeader className="border-b border-border bg-muted/20">
        <SectionHead
          icon={HardDrive}
          title="Export Colab / Kaggle"
          detail="Đóng gói text + cấu hình giọng cho các patch đã chọn ở bảng trên."
          action={
            <span
              className={cn(
                "shrink-0 rounded-md px-2 py-1 font-mono text-[11px]",
                targetIds.length ? "bg-primary/10 text-primary" : "bg-muted text-muted-foreground"
              )}
            >
              {targetIds.length} patch
            </span>
          }
        />
      </CardHeader>

      <CardContent className="space-y-4 pt-5">
        <p className="text-xs text-muted-foreground">
          {usingSelection ? (
            <>
              Dùng <span className="font-medium text-foreground">{targetIds.length} patch đang chọn</span>
              {skipped > 0 && ` (bỏ qua ${skipped} patch đang xử lý)`}.
            </>
          ) : (
            <>
              Chưa chọn patch nào — sẽ export{" "}
              <span className="font-medium text-foreground">toàn bộ {targetIds.length} patch sẵn sàng</span>.
            </>
          )}
        </p>

        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          <Field label="TTS model">
            <select
              className={selectClass}
              value={settings.modelId}
              onChange={(event) => onSettingsChange({ modelId: event.target.value })}
            >
              {ttsModels.length ? (
                ttsModels.map((model) => (
                  <option key={model.id} value={model.id}>
                    {model.name}
                  </option>
                ))
              ) : (
                <option value={settings.modelId}>{settings.modelId}</option>
              )}
            </select>
          </Field>
          <Field label="Voice">
            <select
              className={selectClass}
              value={settings.voiceId}
              onChange={(event) => onSettingsChange({ voiceId: event.target.value })}
            >
              {voiceOptions.length ? (
                voiceOptions.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))
              ) : (
                <option value="">—</option>
              )}
            </select>
          </Field>
          <Field label="Max chars" hint="0 = mặc định">
            <input
              className={fieldClass}
              type="number"
              min="0"
              value={settings.maxChars}
              onChange={(event) => onSettingsChange({ maxChars: event.target.value })}
            />
          </Field>
        </div>

        <div className="flex flex-wrap items-center justify-between gap-3">
          <CheckField
            checked={settings.withEffects}
            onChange={(value) => onSettingsChange({ withEffects: value })}
            label="Chèn hiệu ứng âm thanh"
          />
          <Button type="button" size="sm" variant="outline" onClick={saveSettings} disabled={saving || exporting}>
            <Save className="h-3.5 w-3.5" /> {saving ? "Đang lưu..." : "Lưu cấu hình export"}
          </Button>
        </div>

        <div className="grid grid-cols-1 gap-3 border-t border-border pt-4 sm:grid-cols-2 lg:grid-cols-4">
          <div className="space-y-2">
            <span className="block text-[11px] font-medium text-muted-foreground">Tải về máy</span>
            <Button size="sm" className="w-full" onClick={downloadZip} disabled={exporting || !targetIds.length}>
              <Download className="h-3.5 w-3.5" /> Gói ZIP
            </Button>
          </div>
          <div className="space-y-2">
            <span className="block text-[11px] font-medium text-muted-foreground">Drive Desktop</span>
            <select
              className={selectClass}
              value={syncTargetId}
              onChange={(event) => setSyncTargetId(event.target.value)}
            >
              <option value="">Chọn target</option>
              {syncTargets.map((target) => (
                <option key={target.id} value={target.id}>
                  {target.name}
                </option>
              ))}
            </select>
            <Button
              size="sm"
              variant="outline"
              className="w-full"
              onClick={exportToDesktop}
              disabled={exporting || !targetIds.length}
            >
              Export thư mục
            </Button>
          </div>
          <div className="space-y-2">
            <span className="block text-[11px] font-medium text-muted-foreground">Google Drive API</span>
            <select className={selectClass} value={accountId} onChange={(event) => setAccountId(event.target.value)}>
              <option value="">Chọn account</option>
              {accounts.map((account) => (
                <option key={account.id} value={account.id}>
                  {account.account_email}
                </option>
              ))}
            </select>
            <Button
              size="sm"
              variant="outline"
              className="w-full"
              onClick={exportToDriveApi}
              disabled={exporting || !targetIds.length}
            >
              Export qua API
            </Button>
          </div>
          <div className="space-y-2">
            <span className="block text-[11px] font-medium text-muted-foreground">Kaggle (tự động)</span>
            <Button
              size="sm"
              variant="outline"
              className="w-full"
              onClick={exportToKaggle}
              disabled={exporting || !targetIds.length || !kaggleAccounts.length}
            >
              <Gauge className="h-3.5 w-3.5" /> Chạy trên Kaggle
            </Button>
            {!kaggleAccounts.length && (
              <p className="text-[11px] text-muted-foreground">
                Chưa có tài khoản Kaggle nào — thêm ở trang <span className="font-medium text-foreground">Google Drive &amp; Đồng bộ</span> (tab Kaggle).
              </p>
            )}
          </div>
        </div>

        <div className="border-t border-border pt-3">
          <button
            className="flex w-full items-center gap-2 text-xs font-medium text-muted-foreground hover:text-foreground"
            onClick={() => setCredentialsOpen((open) => !open)}
            aria-expanded={credentialsOpen}
          >
            <ChevronDown className={cn("h-3.5 w-3.5 transition-transform", credentialsOpen && "rotate-180")} />
            <KeyRound className="h-3.5 w-3.5" />
            Kaggle credentials (GDRIVE_CREDS)
          </button>

          {credentialsOpen && (
            <div className="mt-3 space-y-3">
              <div className="flex flex-wrap items-center gap-2">
                <Button size="sm" variant="outline" onClick={loadCredentials}>
                  Lấy credentials
                </Button>
                <Button size="sm" variant="ghost" onClick={copyCredentials} disabled={!credentials}>
                  <Clipboard className="h-3.5 w-3.5" /> Copy
                </Button>
                {!accountId && (
                  <span className="text-[11px] text-muted-foreground">Chọn Drive account ở khối bên trên.</span>
                )}
              </div>
              <p className="text-[11px] text-muted-foreground">
                Export qua API đã nhúng sẵn credentials của account đã chọn vào notebook (biến{" "}
                <code>GDRIVE_CREDS</code> ở Cell 4), nên không cần tạo Kaggle secret nữa. Chỉ lấy JSON ở
                đây khi muốn dùng secret thay vì để credentials nằm trong file .ipynb — notebook có
                credentials phải giữ ở chế độ private.
              </p>
              {credentials && (
                <Textarea className="min-h-24 bg-muted/30 font-mono text-[11px]" value={credentials} readOnly />
              )}
            </div>
          )}
        </div>
      </CardContent>
    </Card>
  );
}
