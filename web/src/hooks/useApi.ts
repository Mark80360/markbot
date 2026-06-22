import { useState, useEffect, useCallback, useRef } from "react";

export function useApi<T>(
  fetcher: () => Promise<T>,
  deps: any[],
): { data: T | null; loading: boolean; error: string | null; refetch: () => void } {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetch = useCallback(() => {
    setLoading(true);
    setError(null);
    fetcher()
      .then(setData)
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, deps);

  useEffect(() => { fetch() }, [fetch]);

  return { data, loading, error, refetch: fetch };
}

/** Standardized feedback message hook with auto-dismiss. */
export function useFeedback(timeout = 3000) {
  const [feedback, setFeedback] = useState<{ message: string; type: "info" | "success" | "error" } | null>(null);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const show = useCallback((message: string, type: "info" | "success" | "error" = "success") => {
    if (timerRef.current) clearTimeout(timerRef.current);
    setFeedback({ message, type });
    timerRef.current = setTimeout(() => setFeedback(null), timeout);
  }, [timeout]);

  useEffect(() => {
    return () => {
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, []);

  return { feedback, show };
}
