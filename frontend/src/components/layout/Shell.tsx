import React, { useState } from "react";
import { Link, NavLink, useLocation } from "react-router-dom";
import {
  LayoutDashboard,
  Library,
  UploadCloud,
  ListOrdered,
  Film,
  FolderKanban,
  Wrench,
  Video,
  HardDrive,
  Database,
  Workflow,
  FileText,
  Sparkles,
  Menu,
  Activity,
} from "lucide-react";
import { Sheet, SheetContent, SheetTrigger } from "@/components/ui/sheet";
import { Button } from "@/components/ui/button";

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
      { to: "/video", label: "Video thành phẩm", icon: Film },
      { to: "/media", label: "Kho tư liệu", icon: FolderKanban },
    ],
  },
  {
    title: "OPERATIONS",
    items: [
      { to: "/tools", label: "Bộ công cụ", icon: Wrench },
      { to: "/youtube", label: "YouTube", icon: Video },
      { to: "/drive", label: "Google Drive", icon: HardDrive },
      { to: "/database-io", label: "Dữ liệu", icon: Database },
      { to: "/flows", label: "Luồng tự động", icon: Workflow },
      { to: "/logs", label: "Nhật ký", icon: FileText },
      { to: "/effects", label: "Hiệu ứng", icon: Sparkles },
    ],
  },
];

function SidebarNav({ onSelect }: { onSelect?: () => void }) {
  const location = useLocation();

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
          <span className="font-bold tracking-wider text-sm text-foreground uppercase">XƯỞNG SÁCH NÓI</span>
          <span className="text-[10px] text-muted-foreground font-mono tracking-tight">CONTROL ROOM v1.0</span>
        </div>
      </Link>

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
      <div className="p-3 border-t border-border bg-muted/30">
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

export function Shell({ children }: { children: React.ReactNode }) {
  const [mobileOpen, setMobileOpen] = useState(false);

  return (
    <div className="min-h-screen bg-background text-foreground flex flex-col md:flex-row">
      {/* Desktop Sidebar */}
      <aside className="hidden md:flex w-64 flex-col fixed top-0 bottom-0 left-0 border-r border-border bg-card z-30">
        <SidebarNav />
      </aside>

      {/* Mobile Top Navigation Header */}
      <header className="md:hidden flex items-center justify-between px-4 py-3 border-b border-border bg-card sticky top-0 z-40">
        <Link to="/" className="flex items-center gap-2.5">
          <div className="h-7 w-7 rounded bg-foreground flex items-center justify-center p-1 shrink-0">
            <img src="/studio-mark.svg" alt="Xưởng Sách Nói" className="h-full w-full invert" />
          </div>
          <span className="font-bold text-xs tracking-wider uppercase">XƯỞNG SÁCH NÓI</span>
        </Link>
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
      </header>

      {/* Main Content Area */}
      <main className="flex-1 md:ml-64 min-w-0 p-4 sm:p-6 lg:p-8 max-w-7xl mx-auto w-full">
        {/* Top Status Bar (Control Room style) */}
        <div className="hidden sm:flex items-center justify-between pb-4 mb-6 border-b border-border text-xs text-muted-foreground font-mono">
          <div className="flex items-center gap-2">
            <Activity className="h-3.5 w-3.5 text-primary" />
            <span>SẢN XUẤT SÁCH NÓI TỰ ĐỘNG</span>
          </div>
          <div className="flex items-center gap-4">
            <span>MÔ TRƯỜNG: LOCAL WORKSPACE</span>
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
