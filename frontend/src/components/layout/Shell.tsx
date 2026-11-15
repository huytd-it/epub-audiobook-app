import React, { useEffect, useMemo, useState } from "react";
import { Link, NavLink, useLocation } from "react-router-dom";
import {
  LayoutDashboard,
  Library,
  UploadCloud,
  ListOrdered,
  HardDrive,
  Database,
  FileText,
  Sparkles,
  Settings2,
  Box,
  Menu,
  Activity,
  Moon,
  Sun,
  Swords,
  FolderSearch,
  Music,
  Image,
  Mic,
  PanelLeftClose,
  PanelLeftOpen,
  Share2,
  BookMarked,
  ArrowRight,
  Clock3,
} from "lucide-react";
import { Sheet, SheetContent, SheetTrigger } from "@/components/ui/sheet";
import { Button } from "@/components/ui/button";
import { getLastBookId, getRecentBooks } from "@/lib/recentBooks";

interface NavItem {
  to: string;
  label: string;
  icon: React.ElementType;
  exact?: boolean;
}

interface NavSection {
  title: string;
  items: NavItem[];
}

const navSections: NavSection[] = [
  {
    title: "WORKSPACE",
    items: [
      { to: "/", label: "Bàn làm việc", icon: LayoutDashboard, exact: true },
      { to: "/books", label: "Thư viện sách", icon: Library },
      { to: "/upload", label: "Nhập EPUB", icon: UploadCloud },
    ],
  },
  {
    title: "PRODUCTION",
    items: [
      { to: "/queue", label: "Hàng đợi sản xuất", icon: ListOrdered },
      { to: "/kaggle", label: "Gói Kaggle CLI", icon: Box },
      { to: "/media-browser", label: "Duyệt Media", icon: FolderSearch },
      { to: "/music", label: "Âm nhạc & Nhạc nền", icon: Music },
      { to: "/photos", label: "Hình ảnh & Background", icon: Image },
      { to: "/voices", label: "Giọng đọc & Voice", icon: Mic },
      { to: "/effects", label: "Hiệu ứng", icon: Sparkles },
      { to: "/gameplay", label: "Video Gameplay", icon: Swords },
    ],
  },
  {
    title: "OPERATIONS",
    items: [
      { to: "/socials", label: "Socials", icon: Share2 },
      { to: "/drive", label: "Google Drive", icon: HardDrive },
      { to: "/database-io", label: "Dữ liệu", icon: Database },
      { to: "/logs", label: "Nhật ký", icon: FileText },
      { to: "/production-defaults", label: "Cấu hình mặc định", icon: Settings2 },
      { to: "/tts-models", label: "Model TTS", icon: Box },
    ],
  },
];

function ResumeEntry({ onSelect, collapsed }: { onSelect?: () => void; collapsed?: boolean }) {
  const [tick, setTick] = useState(0);
  const location = useLocation();
  useEffect(() => {
    const bump = () => setTick((n) => n + 1);
    window.addEventListener("storage", bump);
    window.addEventListener("focus", bump);
    return () => {
      window.removeEventListener("storage", bump);
      window.removeEventListener("focus", bump);
    };
  }, []);
  // re-read on tick + on route change (after navigating to a book, sidebar updates ngay)
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const last = useMemo(() => {
    const id = getLastBookId();
    const recents = getRecentBooks();
    const r = id ? recents.find((x) => x.id === id) || recents[0] : recents[0];
    return r || null;
  }, [tick, location.pathname]);
  if (!last) return null;
  const isActive = location.pathname === `/books/${last.id}` || location.pathname.startsWith(`/books/${last.id}/`);
  if (collapsed) {
    return (
      <Link
        to={`/books/${last.id}`}
        onClick={onSelect}
        title={`Quay lại: ${last.title}`}
        aria-label={`Quay lại ${last.title}`}
        className={`flex items-center justify-center px-3 py-2 text-xs font-medium rounded-md transition-colors border ${isActive ? "bg-primary text-primary-foreground border-primary" : "bg-lime/15 border-lime/30 text-foreground hover:bg-lime/25"}`}
      >
        <BookMarked className="h-4 w-4 shrink-0" />
      </Link>
    );
  }
  return (
    <Link
      to={`/books/${last.id}`}
      onClick={onSelect}
      className={`flex items-center gap-2.5 px-3 py-2.5 rounded-md border text-xs transition-colors ${isActive ? "bg-primary text-primary-foreground border-primary" : "bg-lime/10 border-lime/30 hover:bg-lime/20 text-foreground"}`}
    >
      <span className={`h-7 w-7 rounded-md flex items-center justify-center shrink-0 ${isActive ? "bg-primary-foreground/15" : "bg-lime/30"}`}>
        <BookMarked className="h-3.5 w-3.5" />
      </span>
      <span className="min-w-0 flex-1">
        <span className="flex items-center gap-1 text-[10px] font-mono font-semibold tracking-wider opacity-70">
          <Clock3 className="h-3 w-3" /> TIẾP TỤC
        </span>
        <span className="block truncate font-semibold leading-tight">{last.title}</span>
        <span className={`block truncate text-[11px] font-mono ${isActive ? "text-primary-foreground/70" : "text-muted-foreground"}`}>#{last.id}</span>
      </span>
      <ArrowRight className="h-3.5 w-3.5 shrink-0 opacity-60" />
    </Link>
  );
}

function SidebarNav({ onSelect, collapsed = false }: { onSelect?: () => void; collapsed?: boolean }) {
  const location = useLocation();

  if (collapsed) {
    return (
      <div className="flex flex-col h-full">
        {/* Brand Header */}
        <Link
          to="/"
          className="flex items-center justify-center px-2 py-4 border-b border-border hover:opacity-90 transition-opacity"
          onClick={onSelect}
        >
          <div className="h-9 w-9 rounded-md flex items-center justify-center p-1.5 shrink-0">
            <img src="/studio-mark.svg" alt="Xưởng Sách Nói" className="h-full w-full invert" />
          </div>
        </Link>

        {/* Quick resume */}
        <div className="px-2 pt-3">
          <ResumeEntry onSelect={onSelect} collapsed />
        </div>

        {/* Navigation Sections */}
        <div className="flex-1 overflow-y-auto py-4 px-2 space-y-6">
          {navSections.map((section) => (
            <div key={section.title} className="space-y-1">
              <div className="flex justify-center">
                <div className="h-px w-6 bg-muted" title={section.title} />
              </div>
              <nav className="space-y-0.5">
                {section.items.map((item) => {
                  const Icon = item.icon;
                  const isActive = item.exact
                    ? location.pathname === item.to
                    : location.pathname === item.to ||
                      (item.to !== "/" && location.pathname.startsWith(item.to));

                  return (
                    <NavLink
                      key={item.to}
                      to={item.to}
                      onClick={onSelect}
                      title={item.label}
                      aria-label={item.label}
                      className={`flex items-center justify-center px-3 py-2 text-xs font-medium rounded-md transition-colors ${
                        isActive
                          ? "bg-primary text-primary-foreground font-semibold shadow-xs"
                          : "text-foreground/80 hover:bg-muted hover:text-foreground"
                      }`}
                    >
                      <Icon className={`h-4 w-4 shrink-0 ${isActive ? "text-primary-foreground" : "text-muted-foreground"}`} />
                    </NavLink>
                  );
                })}
              </nav>
            </div>
          ))}
        </div>

        {/* Studio Operational Status */}
        <div className="p-2.5 border-t border-border bg-muted/30 flex justify-center">
          <div className="flex items-center gap-2 text-xs text-muted-foreground" title="Backend cục bộ">
            <span className="relative flex h-2 w-2">
              <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-lime opacity-75"></span>
              <span className="relative inline-flex rounded-full h-2 w-2 bg-lime"></span>
            </span>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="flex flex-col h-full">
      {/* Brand Header */}
      <Link
        to="/"
        className="flex items-center gap-3 px-3 py-4 border-b border-border hover:opacity-90 transition-opacity"
        onClick={onSelect}
      >
        <div className="h-9 w-9 rounded-md flex items-center justify-center p-1.5 shrink-0">
          <img src="/studio-mark.svg" alt="Xưởng Sách Nói" className="h-full w-full invert" />
        </div>
        <div className="flex flex-col leading-tight">
          <span className="font-display font-extrabold tracking-tight text-sm text-foreground leading-none">Xưởng Sách Nói</span>
          <span className="text-[10px] text-muted-foreground font-mono tracking-tight">CONTROL ROOM v1.0</span>
        </div>
      </Link>

      {/* Quick resume — ebook đang làm */}
      <div className="px-2 pt-3">
        <ResumeEntry onSelect={onSelect} />
      </div>

      {/* Navigation Sections */}
      <div className="flex-1 overflow-y-auto py-4 px-2 space-y-6">
        {navSections.map((section) => (
          <div key={section.title} className="space-y-1">
            <h4 className="px-3 text-[11px] font-mono font-semibold tracking-wider text-muted-foreground/80 uppercase">
              {section.title}
            </h4>
            <nav className="space-y-0.5">
              {section.items.map((item) => {
                const Icon = item.icon;
                const isActive = item.exact
                  ? location.pathname === item.to
                  : location.pathname === item.to ||
                    (item.to !== "/" && location.pathname.startsWith(item.to));

                return (
                  <NavLink
                    key={item.to}
                    to={item.to}
                    onClick={onSelect}
                    className={`flex items-center gap-2.5 px-3 py-2 text-xs font-medium rounded-md transition-colors ${
                      isActive
                        ? "bg-primary text-primary-foreground font-semibold shadow-xs"
                        : "text-foreground/80 hover:bg-muted hover:text-foreground"
                    }`}
                  >
                    <Icon className={`h-4 w-4 shrink-0 ${isActive ? "text-primary-foreground" : "text-muted-foreground"}`} />
                    <span>{item.label}</span>
                  </NavLink>
                );
              })}
            </nav>
          </div>
        ))}
      </div>

      {/* Studio Operational Status */}
      <div className="p-2.5 border-t border-border bg-muted/30">
        <div className="flex items-center gap-2 text-xs text-muted-foreground">
          <span className="relative flex h-2 w-2">
            <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-lime opacity-75"></span>
            <span className="relative inline-flex rounded-full h-2 w-2 bg-lime"></span>
          </span>
          <span className="font-medium text-foreground text-[11px]">Backend cục bộ</span>
        </div>
        <p className="text-[10px] text-muted-foreground font-mono mt-0.5">FastAPI + Tauri Engine</p>
      </div>
    </div>
  );
}

type Theme = "light" | "dark";

function getInitialTheme(): Theme {
  const savedTheme = window.localStorage.getItem("studio-theme");
  if (savedTheme === "light" || savedTheme === "dark") return savedTheme;
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

export function Shell({ children }: { children: React.ReactNode }) {
  const [mobileOpen, setMobileOpen] = useState(false);
  const [theme, setTheme] = useState<Theme>(getInitialTheme);
  const [collapsed, setCollapsed] = useState(() => {
    const saved = window.localStorage.getItem("studio-sidebar-collapsed");
    return saved === "1";
  });

  useEffect(() => {
    document.documentElement.classList.toggle("dark", theme === "dark");
    window.localStorage.setItem("studio-theme", theme);
  }, [theme]);

  useEffect(() => {
    window.localStorage.setItem("studio-sidebar-collapsed", collapsed ? "1" : "0");
  }, [collapsed]);

  const toggleTheme = () => setTheme((current) => current === "dark" ? "light" : "dark");
  const toggleSidebar = () => setCollapsed((current) => !current);
  const themeLabel = theme === "dark" ? "Chuyển sang giao diện sáng" : "Chuyển sang giao diện tối";

  return (
    <div className="min-h-screen bg-background text-foreground flex flex-col md:flex-row">
      {/* Desktop Sidebar */}
      <aside
        className={`hidden md:flex ${collapsed ? "w-16" : "w-64"} flex-col fixed top-0 bottom-0 left-0 border-r border-border bg-card z-30 transition-[width] duration-300`}
      >
        <SidebarNav collapsed={collapsed} />
        <button
          onClick={toggleSidebar}
          aria-label={collapsed ? "Mở rộng panel" : "Thu gọn panel"}
          title={collapsed ? "Mở rộng panel" : "Thu gọn panel"}
          className="flex items-center justify-center gap-2 p-2.5 border-t border-border text-muted-foreground hover:text-foreground hover:bg-muted transition-colors"
        >
          {collapsed ? <PanelLeftOpen className="h-4 w-4" /> : <PanelLeftClose className="h-4 w-4" />}
          {!collapsed && <span className="text-[10px] font-mono uppercase tracking-wide">Thu gọn</span>}
        </button>
      </aside>

      {/* Mobile Top Navigation Header */}
      <header className="md:hidden flex items-center justify-between px-4 py-3 border-b border-border bg-card sticky top-0 z-40">
        <Link to="/" className="flex items-center gap-2.5">
          <div className="h-7 w-7 rounded bg-foreground flex items-center justify-center p-1 shrink-0">
            <img src="/studio-mark.svg" alt="Xưởng Sách Nói" className="h-full w-full invert" />
          </div>
          <span className="font-display font-extrabold text-xs tracking-tight leading-none">Xưởng Sách Nói</span>
        </Link>
        <div className="flex items-center gap-1">
          <Button variant="ghost" size="icon" onClick={toggleTheme} aria-label={themeLabel} title={themeLabel}>
            {theme === "dark" ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
          </Button>
          <Sheet open={mobileOpen} onOpenChange={setMobileOpen}>
            <SheetTrigger asChild>
              <Button variant="ghost" size="icon" aria-label="Mở menu">
                <Menu className="h-5 w-5" />
              </Button>
            </SheetTrigger>
            <SheetContent side="left" className="p-0 w-72">
              <SidebarNav onSelect={() => setMobileOpen(false)} />
            </SheetContent>
          </Sheet>
        </div>
      </header>

      {/* Main Content Area */}
      <main className={`flex-1 ${collapsed ? "md:ml-16" : "md:ml-64"} min-w-0 p-4 sm:p-6 lg:p-8 max-w-7xl mx-auto w-full transition-[margin] duration-300`}>
        {/* Top Status Bar (Control Room style) */}
        <div className="hidden sm:flex items-center justify-between pb-4 mb-6 border-b border-border text-xs text-muted-foreground font-mono">
          <div className="flex items-center gap-2">
            <Activity className="h-3.5 w-3.5 text-primary" />
            <span>SẢN XUẤT SÁCH NÓI TỰ ĐỘNG</span>
          </div>
          <div className="flex items-center gap-3">
            <span>MÔI TRƯỜNG: LOCAL WORKSPACE</span>
            <Button
              variant="ghost"
              size="sm"
              onClick={toggleTheme}
              aria-label={themeLabel}
              title={themeLabel}
              className="h-7 gap-1.5 px-2 font-mono text-[10px]"
            >
              {theme === "dark" ? <Sun className="h-3.5 w-3.5" /> : <Moon className="h-3.5 w-3.5" />}
              {theme === "dark" ? "SÁNG" : "TỐI"}
            </Button>
            <span className="flex items-center gap-1.5 text-foreground font-medium">
              <span className="h-2 w-2 rounded-full bg-emerald-500" />
              SẴN SÀNG
            </span>
          </div>
        </div>

        {children}
      </main>
    </div>
  );
}
