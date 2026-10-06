import React, { useEffect, useState } from "react";
import { Check, Star, Unplug } from "lucide-react";
import { api, del, patchJson, postJson, EgressEndpoint, SocialAccount } from "@/api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { confirmDialog } from "@/components/ui/confirm";
import { showToast } from "@/components/ui/toast";
import { PlatformMeta, selectClass } from "./platforms";

/**
 * Bộ chọn + quản lý tài khoản dùng chung cho mọi mạng: chọn tài khoản đang làm việc,
 * đặt mặc định, đổi nhãn, ghim outbound proxy, ngắt kết nối. `addAction` là nút/khối
 * "thêm tài khoản" riêng của từng mạng (OAuth với YouTube, form dán token với FB/TikTok).
 */
export function AccountsPanel({
  platform,
  accounts,
  selectedId,
  onSelect,
  proxies,
  onChanged,
  addAction,
}: {
  platform: PlatformMeta;
  accounts: SocialAccount[];
  selectedId: number | null;
  onSelect: (accountId: number) => void;
  proxies: EgressEndpoint[];
  onChanged: () => void;
  addAction?: React.ReactNode;
}) {
  const selected = accounts.find((account) => account.id === selectedId) ?? null;
  const [label, setLabel] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    setLabel(selected?.label ?? "");
  }, [selected?.id, selected?.label]);

  const run = async (action: () => Promise<unknown>, done?: string) => {
    setBusy(true);
    try {
      await action();
      if (done) showToast(done, "success");
      onChanged();
    } catch (error: any) {
      showToast(error.message || "Thao tác thất bại", "error");
    } finally {
      setBusy(false);
    }
  };

  const disconnect = async (account: SocialAccount) => {
    const confirmed = await confirmDialog({
      title: `Ngắt kết nối ${platform.accountNoun} "${account.label}"?`,
      message:
        "Token của tài khoản này bị xoá khỏi app. Video đang chờ đăng bằng tài khoản này sẽ báo lỗi chưa kết nối; nội dung đã đăng trên mạng không bị ảnh hưởng.",
      confirmLabel: "Ngắt kết nối",
    });
    if (!confirmed) return;
    await run(() => del(`/socials/api/accounts/${account.id}`), "Đã ngắt kết nối");
  };

  return (
    <Card>
      <CardContent className="space-y-3 p-4">
        <div className="flex flex-wrap items-center gap-2">
          {accounts.length === 0 && (
            <span className="text-xs text-muted-foreground">
              Chưa kết nối {platform.accountNoun} {platform.label} nào.
            </span>
          )}
          {accounts.map((account) => (
            <Button
              key={account.id}
              size="sm"
              variant={account.id === selectedId ? "default" : "outline"}
              onClick={() => onSelect(account.id)}
              title={account.external_id || undefined}
            >
              {account.is_default && <Star className="h-3.5 w-3.5 fill-current" />}
              {account.label || account.external_id || `#${account.id}`}
              {account.status === "error" && <span className="h-1.5 w-1.5 rounded-full bg-red-500" />}
            </Button>
          ))}
          {addAction && <div className="ml-auto flex items-center gap-2">{addAction}</div>}
        </div>

        {selected && (
          <div className="flex flex-wrap items-end gap-3 border-t border-border pt-3">
            <label className="flex flex-col gap-1 text-[11px] font-mono uppercase tracking-wide text-muted-foreground">
              Nhãn
              <div className="flex items-center gap-1">
                <Input
                  value={label}
                  onChange={(event) => setLabel(event.target.value)}
                  className="h-9 w-56 text-xs"
                />
                <Button
                  size="sm"
                  variant="outline"
                  disabled={busy || label.trim() === selected.label}
                  onClick={() =>
                    run(() => patchJson(`/socials/api/accounts/${selected.id}`, { label: label.trim() }), "Đã đổi nhãn")
                  }
                >
                  <Check className="h-3.5 w-3.5" />
                </Button>
              </div>
            </label>

            <label className="flex flex-col gap-1 text-[11px] font-mono uppercase tracking-wide text-muted-foreground">
              Outbound proxy
              <select
                className={selectClass}
                disabled={busy}
                value={selected.egress_endpoint_id ?? ""}
                onChange={(event) =>
                  run(
                    () =>
                      patchJson(`/socials/api/accounts/${selected.id}`, {
                        egress_endpoint_id: event.target.value ? Number(event.target.value) : null,
                      }),
                    "Đã cập nhật proxy của tài khoản"
                  )
                }
              >
                <option value="">Theo policy của {platform.label}</option>
                {proxies.map((proxy) => (
                  <option key={proxy.id} value={proxy.id}>
                    {proxy.label || proxy.url}
                    {proxy.region ? ` · ${proxy.region}` : ""}
                    {proxy.enabled ? "" : " (đang tắt)"}
                  </option>
                ))}
              </select>
            </label>

            <div className="ml-auto flex items-center gap-2">
              {selected.is_default ? (
                <Badge variant="lime">Mặc định</Badge>
              ) : (
                <Button
                  size="sm"
                  variant="outline"
                  disabled={busy}
                  onClick={() =>
                    run(() => postJson(`/socials/api/accounts/${selected.id}/default`, {}), "Đã đặt làm mặc định")
                  }
                >
                  <Star className="h-3.5 w-3.5" />
                  Đặt làm mặc định
                </Button>
              )}
              <Button size="sm" variant="ghost" disabled={busy} onClick={() => disconnect(selected)}>
                <Unplug className="h-3.5 w-3.5" />
                Ngắt kết nối
              </Button>
            </div>

            {selected.last_error && (
              <p className="w-full text-xs text-red-600">Lỗi gần nhất: {selected.last_error}</p>
            )}
            <p className="w-full text-[11px] text-muted-foreground">
              Tài khoản mặc định là tài khoản được dùng khi một luồng không chọn gì (auto-upload của sách chưa
              gán kênh, đăng short không chọn tài khoản).
            </p>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

/** Tải lại danh sách proxy cho ô "Outbound proxy" — dùng chung giữa các tab mạng. */
export const loadProxies = () =>
  api<{ endpoints: EgressEndpoint[] }>("/socials/api/network").then((state) =>
    state.endpoints.filter((endpoint) => endpoint.kind === "proxy")
  );
