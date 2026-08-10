import React, { useEffect, useMemo, useRef, useState } from "react";
import { Clipboard, Download, HardDrive, KeyRound } from "lucide-react";
import { api, DriveAccount, DriveTarget, Patch, post } from "@/api";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Textarea } from "@/components/ui/textarea";
import { AudioSettings, TtsModel, VoiceOption, downloadForm, errorText } from "./types";
import { CheckField, Field, SectionHead, checkboxClass, fieldClass, selectClass } from "./parts";

export function ExportPanel({
  bookId,
  patches,
  accounts,
  syncTargets,
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
  accounts: DriveAccount[];
  syncTargets: DriveTarget[];
  settings: AudioSettings;
  onSettingsChange: (patch: Partial<AudioSettings>) => void;
  ttsModels: TtsModel[];
  voiceOptions: VoiceOption[];
  onMessage: (message: string) => void;
  onRefresh: () => Promise<void> | void;
  onBusyChange: (busy: boolean) => void;
}) {
  const exportable = useMemo(() => patches.filter((patch) => patch.status !== "processing"), [patches]);
  const availableIds = useMemo(() => exportable.map((patch) => patch.id), [exportable]);

  const [exportIds, setExportIds] = useState<number[]>([]);
  const [syncTargetId, setSyncTargetId] = useState("");
  const [accountId, setAccountId] = useState("");
  const [exporting, setExporting] = useState(false);
  const [credentials, setCredentials] = useState("");
  const seeded = useRef(false);

  // Giữ nguyên lựa chọn của người dùng qua mỗi nhịp polling, chỉ bỏ patch đã biến mất.
  useEffect(() => {
    setExportIds((current) => {
      if (!seeded.current && availableIds.length) {
        seeded.current = true;
        return availableIds;
      }
      const next = current.filter((id) => availableIds.includes(id));
      return next.length === current.length ? current : next;
    });
  }, [availableIds]);

  const toggle = (patchId: number) =>
    setExportIds((current) =>
      current.includes(patchId) ? current.filter((id) => id !== patchId) : [...current, patchId]
    );

  const buildForm = () => {
    const form = new FormData();
    exportIds.forEach((id) => form.append("patch_ids", String(id)));
    form.set("model_id", settings.modelId);
    form.set("voice_id", settings.voiceId);
    form.set("max_chars", settings.maxChars || "0");
    form.set("with_effects", settings.withEffects ? "1" : "0");
    return form;
  };

  const runExport = async (action: (form: FormData) => Promise<unknown>, done: string, refresh = true) => {
    if (!exportIds.length) {
      onMessage("Chọn ít nhất một patch để export.");
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

  const downloadZip = () =>
    runExport((form) => downloadForm(`/books/${bookId}/patches/export-batch/download`, form), "Đã tải gói ZIP.", false);

  const exportToDesktop = () => {
    if (!syncTargetId) {
      onMessage("Chọn Drive Desktop target.");
      return;
    }
    return runExport((form) => {
      form.set("sync_target_id", syncTargetId);
      return post(`/books/${bookId}/patches/export-batch`, form);
    }, "Đã export sang Drive Desktop.");
  };

  const exportToDriveApi = () => {
    if (!accountId) {
      onMessage("Chọn Google Drive account.");
      return;
    }
    return runExport((form) => {
      form.set("account_id", accountId);
      return post(`/books/${bookId}/patches/export-batch-api`, form);
    }, "Đã export qua Google Drive API.");
  };

  const loadCredentials = async () => {
    if (!accountId) {
      onMessage("Chọn account trước khi lấy credentials.");
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
    <div className="space-y-5">
      <Card>
        <CardHeader className="border-b border-border bg-muted/20">
          <SectionHead
            icon={HardDrive}
            title="Export Colab / Kaggle"
            detail="Đóng gói text + cấu hình giọng để tổng hợp ở nơi khác."
            action={
              <span className="shrink-0 font-mono text-[11px] text-muted-foreground">
                {exportIds.length}/{exportable.length} patch
              </span>
            }
          />
        </CardHeader>

        <CardContent className="space-y-4 pt-5">
          <div className="overflow-hidden rounded-md border border-border">
            {exportable.length === 0 ? (
              <div className="p-4 text-xs text-muted-foreground">Không có patch sẵn sàng để export.</div>
            ) : (
              <>
                <label className="flex items-center gap-2 border-b border-border bg-muted/30 px-3 py-2 text-xs font-medium">
                  <input
                    type="checkbox"
                    className={checkboxClass}
                    checked={exportIds.length === exportable.length}
                    onChange={(event) => setExportIds(event.target.checked ? availableIds : [])}
                  />
                  Chọn tất cả ({exportable.length})
                </label>
                <div className="max-h-48 overflow-auto">
                  {exportable.map((patch) => (
                    <label
                      key={patch.id}
                      className="flex cursor-pointer items-center gap-3 border-b border-border px-3 py-2 text-xs last:border-0 hover:bg-muted/30"
                    >
                      <input
                        type="checkbox"
                        className={checkboxClass}
                        checked={exportIds.includes(patch.id)}
                        onChange={() => toggle(patch.id)}
                      />
                      <span className="font-mono text-muted-foreground">#{patch.patch_index + 1}</span>
                      <span className="min-w-0 flex-1 truncate">{patch.name || `Patch ${patch.patch_index + 1}`}</span>
                    </label>
                  ))}
                </div>
              </>
            )}
          </div>

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

          <CheckField
            checked={settings.withEffects}
            onChange={(value) => onSettingsChange({ withEffects: value })}
            label="Chèn hiệu ứng âm thanh"
          />

          <div className="grid grid-cols-1 gap-3 border-t border-border pt-4 sm:grid-cols-3">
            <div className="space-y-2">
              <span className="text-[11px] font-medium text-muted-foreground">Tải về máy</span>
              <Button size="sm" className="w-full" onClick={downloadZip} disabled={exporting}>
                <Download className="h-3.5 w-3.5" /> Gói ZIP
              </Button>
            </div>
            <div className="space-y-2">
              <span className="text-[11px] font-medium text-muted-foreground">Drive Desktop</span>
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
              <Button size="sm" variant="outline" className="w-full" onClick={exportToDesktop} disabled={exporting}>
                Export thư mục
              </Button>
            </div>
            <div className="space-y-2">
              <span className="text-[11px] font-medium text-muted-foreground">Google Drive API</span>
              <select className={selectClass} value={accountId} onChange={(event) => setAccountId(event.target.value)}>
                <option value="">Chọn account</option>
                {accounts.map((account) => (
                  <option key={account.id} value={account.id}>
                    {account.account_email}
                  </option>
                ))}
              </select>
              <Button size="sm" variant="outline" className="w-full" onClick={exportToDriveApi} disabled={exporting}>
                Export qua API
              </Button>
            </div>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="border-b border-border bg-muted/20">
          <SectionHead
            icon={KeyRound}
            title="Kaggle credentials"
            detail="Lấy GDRIVE_CREDS của account đã chọn ở trên để dán vào secret của Kaggle."
          />
        </CardHeader>
        <CardContent className="space-y-3 pt-5">
          <div className="flex flex-wrap items-center gap-2">
            <Button size="sm" variant="outline" onClick={loadCredentials}>
              <KeyRound className="h-3.5 w-3.5" /> Lấy credentials
            </Button>
            <Button size="sm" variant="ghost" onClick={copyCredentials} disabled={!credentials}>
              <Clipboard className="h-3.5 w-3.5" /> Copy
            </Button>
            {!accountId && <span className="text-[11px] text-muted-foreground">Chọn Drive account ở khối trên.</span>}
          </div>
          {credentials && (
            <Textarea className="min-h-24 bg-muted/30 font-mono text-[11px]" value={credentials} readOnly />
          )}
        </CardContent>
      </Card>
    </div>
  );
}
