import type React from "react";
import { Flag, Music2, Video } from "lucide-react";
import type { EgressEndpoint, SocialAccount, SocialPlatform } from "@/api";

/** Thứ mọi workspace nhận từ SocialsPage. `account` null = mạng chưa có tài khoản nào. */
export type WorkspaceProps = {
  account: SocialAccount | null;
  accounts: SocialAccount[];
  /** OAuth client của mạng đã có trong .env chưa (chỉ YouTube cần). */
  configured: boolean;
  /** Gọi sau khi thêm/sửa tài khoản để hub tải lại danh sách. */
  onChanged: (selectId?: number) => void;
};

export type PlatformMeta = {
  id: SocialPlatform;
  label: string;
  icon: React.ElementType;
  /** Một tài khoản của mạng này gọi là gì, để ghi nhãn cho đúng. */
  accountNoun: string;
  subtitle: string;
};

/**
 * Các mạng của Socials hub, theo thứ tự tab. Thêm mạng mới = thêm một dòng ở đây,
 * một workspace trong SocialsPage (WORKSPACES) và platform tương ứng ở backend
 * (app/social_accounts.py PLATFORMS).
 */
export const PLATFORMS: PlatformMeta[] = [
  {
    id: "youtube",
    label: "YouTube",
    icon: Video,
    accountNoun: "kênh",
    subtitle: "Tải video sách nói, quản lý tiến trình upload, playlist và video trên kênh.",
  },
  {
    id: "facebook",
    label: "Facebook",
    icon: Flag,
    accountNoun: "Page",
    subtitle: "Đăng short lên Page (Reels), xem video đã đăng và lịch sử đăng.",
  },
  {
    id: "tiktok",
    label: "TikTok",
    icon: Music2,
    accountNoun: "tài khoản",
    subtitle: "Đăng short qua Content Posting API và theo dõi trạng thái publish.",
  },
];

export const selectClass =
  "h-9 rounded-md border border-input bg-background px-2 text-xs text-foreground focus:outline-hidden focus:ring-2 focus:ring-ring disabled:opacity-50";

export const proxiesOf = (endpoints: EgressEndpoint[]) =>
  endpoints.filter((endpoint) => endpoint.kind === "proxy");
