import React, { useCallback, useEffect, useState } from "react";
import { Navigate, useLocation, useNavigate } from "react-router-dom";
import { ExternalLink, RefreshCw } from "lucide-react";
import { api, EgressEndpoint, SocialPlatform, SocialsOverview } from "@/api";
import { EmptyState, Header, LoadingState } from "@/components/common/Header";
import { Button } from "@/components/ui/button";
import { showToast } from "@/components/ui/toast";
import { AccountsPanel, loadProxies } from "./AccountsPanel";
import { PLATFORMS, WorkspaceProps } from "./platforms";
import { FacebookWorkspace } from "./facebook/FacebookWorkspace";
import { TikTokWorkspace } from "./tiktok/TikTokWorkspace";
import { YouTubeWorkspace } from "./youtube/YouTubeWorkspace";

const SELECTION_KEY = "studio-socials-account";

/** YouTube kết nối bằng OAuth nên không có form: chưa có kênh thì chỉ hướng dẫn kết nối. */
function YouTubePanel({ account, configured }: WorkspaceProps) {
  if (!configured) {
    return (
      <EmptyState text="YouTube chưa được cấu hình: đặt YOUTUBE_CLIENT_ID và YOUTUBE_CLIENT_SECRET trong .env rồi khởi động lại." />
    );
  }
  if (!account) {
    return <EmptyState text='Chưa kết nối kênh nào. Bấm "Kết nối kênh YouTube" để cấp quyền cho kênh đầu tiên.' />;
  }
  // key: đổi kênh là mount lại, không để state của kênh trước rò sang kênh sau.
  return <YouTubeWorkspace key={account.id} accountId={account.id} />;
}

const WORKSPACES: Record<SocialPlatform, React.ComponentType<WorkspaceProps>> = {
  youtube: YouTubePanel,
  facebook: FacebookWorkspace,
  tiktok: TikTokWorkspace,
};

function readSelection(): Partial<Record<SocialPlatform, number>> {
  try {
    return JSON.parse(window.localStorage.getItem(SELECTION_KEY) || "{}") || {};
  } catch {
    return {};
  }
}

export function SocialsPage() {
  const location = useLocation();
  const navigate = useNavigate();
  const tab = location.pathname.split("/")[2] || "";
  const platform = PLATFORMS.find((entry) => entry.id === tab);

  const [overview, setOverview] = useState<SocialsOverview | null>(null);
  const [proxies, setProxies] = useState<EgressEndpoint[]>([]);
  const [selection, setSelection] = useState(readSelection);

  const select = useCallback((platformId: SocialPlatform, accountId: number) => {
    setSelection((current) => {
      const next = { ...current, [platformId]: accountId };
      window.localStorage.setItem(SELECTION_KEY, JSON.stringify(next));
      return next;
    });
  }, []);

  const reload = useCallback(() => {
    api<SocialsOverview>("/socials/api/overview")
      .then(setOverview)
      .catch((error) => showToast(error.message, "error"));
    loadProxies().then(setProxies).catch(() => setProxies([]));
  }, []);

  useEffect(reload, [reload, tab]);

  // OAuth mở ở tab khác; quay lại cửa sổ này thì danh sách kênh phải tự cập nhật.
  useEffect(() => {
    window.addEventListener("focus", reload);
    return () => window.removeEventListener("focus", reload);
  }, [reload]);

  // Kết quả OAuth của YouTube quay về /socials/youtube?connected=1 hoặc ?error=...
  useEffect(() => {
    const params = new URLSearchParams(location.search);
    if (!params.has("connected") && !params.has("error")) return;
    if (params.has("connected")) showToast("Đã kết nối kênh YouTube", "success");
    else showToast(`Kết nối thất bại: ${params.get("error")}`, "error");
    navigate(location.pathname, { replace: true });
  }, [location.search, location.pathname, navigate]);

  if (!platform) {
    return <Navigate to={`/socials/${PLATFORMS[0].id}`} replace />;
  }

  const state = overview ? overview[platform.id] : null;
  const accounts = state?.accounts ?? [];
  const selectedId =
    accounts.some((account) => account.id === selection[platform.id])
      ? selection[platform.id]!
      : accounts.find((account) => account.is_default)?.id ?? accounts[0]?.id ?? null;
  const account = accounts.find((entry) => entry.id === selectedId) ?? null;
  const Workspace = WORKSPACES[platform.id];

  return (
    <div className="space-y-6">
      <Header
        title="Socials"
        subtitle={platform.subtitle}
        action={
          <Button variant="ghost" size="sm" onClick={reload}>
            <RefreshCw className="h-4 w-4" />
            Tải lại
          </Button>
        }
      />

      <div className="flex flex-wrap items-center gap-2 border-b border-border pb-2">
        {PLATFORMS.map((entry) => {
          const Icon = entry.icon;
          const count = overview?.[entry.id].accounts.length ?? 0;
          return (
            <Button
              key={entry.id}
              size="sm"
              variant={entry.id === tab ? "default" : "ghost"}
              onClick={() => navigate(`/socials/${entry.id}`)}
            >
              <Icon className="h-4 w-4" />
              {entry.label} ({count})
            </Button>
          );
        })}
      </div>

      {!overview ? (
        <LoadingState />
      ) : (
        <>
          <AccountsPanel
            platform={platform}
            accounts={accounts}
            selectedId={selectedId}
            onSelect={(accountId) => select(platform.id, accountId)}
            proxies={proxies}
            onChanged={reload}
            addAction={
              platform.id === "youtube" && state?.configured ? (
                <Button variant="outline" size="sm" onClick={() => window.open("/youtube/connect", "_blank")}>
                  <ExternalLink className="h-4 w-4" />
                  Kết nối kênh YouTube
                </Button>
              ) : undefined
            }
          />
          <Workspace
            account={account}
            accounts={accounts}
            configured={state?.configured ?? false}
            onChanged={(selectId) => {
              if (selectId != null) select(platform.id, selectId);
              reload();
            }}
          />
        </>
      )}
    </div>
  );
}
