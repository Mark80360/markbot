import { useEffect, useState } from "react";

export const MOBILE_QUERY = "(max-width: 767px)";

/** Subscribe to a CSS media query with SSR-safe initial state. */
export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState<boolean>(() =>
    typeof window === "undefined" ? false : window.matchMedia(query).matches,
  );

  useEffect(() => {
    const mql = window.matchMedia(query);
    const onChange = (event: MediaQueryListEvent) => setMatches(event.matches);
    setMatches(mql.matches);
    mql.addEventListener("change", onChange);
    return () => mql.removeEventListener("change", onChange);
  }, [query]);

  return matches;
}

/** True on phone-sized viewports, where the sidebar becomes an overlay drawer. */
export function useIsMobile(): boolean {
  return useMediaQuery(MOBILE_QUERY);
}
