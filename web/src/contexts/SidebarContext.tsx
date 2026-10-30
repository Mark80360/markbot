import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { useIsMobile } from "@/hooks/useMediaQuery";

const COLLAPSE_KEY = "markbot-sidebar-collapsed";

interface SidebarContextValue {
  /** Desktop only: icon-less rail / fully hidden column. */
  collapsed: boolean;
  setCollapsed: (value: boolean) => void;
  toggle: () => void;
  /** Mobile only: off-canvas drawer visibility. */
  mobileOpen: boolean;
  setMobileOpen: (value: boolean) => void;
  toggleMobile: () => void;
  closeMobile: () => void;
  isMobile: boolean;
}

const SidebarContext = createContext<SidebarContextValue | null>(null);

function readCollapsed(): boolean {
  try {
    return localStorage.getItem(COLLAPSE_KEY) === "1";
  } catch {
    return false;
  }
}

export function SidebarProvider({ children }: { children: ReactNode }) {
  const isMobile = useIsMobile();
  const [collapsed, setCollapsedState] = useState(readCollapsed);
  const [mobileOpen, setMobileOpen] = useState(false);

  const setCollapsed = useCallback((value: boolean) => {
    setCollapsedState(value);
    try {
      localStorage.setItem(COLLAPSE_KEY, value ? "1" : "0");
    } catch {
      /* storage unavailable — keep in-memory state */
    }
  }, []);

  const toggle = useCallback(() => setCollapsedState((prev) => {
    const next = !prev;
    try {
      localStorage.setItem(COLLAPSE_KEY, next ? "1" : "0");
    } catch {
      /* ignore */
    }
    return next;
  }), []);

  const toggleMobile = useCallback(() => setMobileOpen((prev) => !prev), []);
  const closeMobile = useCallback(() => setMobileOpen(false), []);

  // 视口从窄屏切到宽屏时，抽屉状态要清掉，否则遮罩会残留在桌面上。
  useEffect(() => {
    if (!isMobile) setMobileOpen(false);
  }, [isMobile]);

  const value = useMemo<SidebarContextValue>(
    () => ({
      collapsed,
      setCollapsed,
      toggle,
      mobileOpen,
      setMobileOpen,
      toggleMobile,
      closeMobile,
      isMobile,
    }),
    [collapsed, setCollapsed, toggle, mobileOpen, toggleMobile, closeMobile, isMobile],
  );

  return <SidebarContext.Provider value={value}>{children}</SidebarContext.Provider>;
}

export function useSidebar(): SidebarContextValue {
  const ctx = useContext(SidebarContext);
  if (!ctx) throw new Error("useSidebar must be used within SidebarProvider");
  return ctx;
}
