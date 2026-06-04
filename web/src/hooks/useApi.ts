import { useState, useEffect, useCallback } from "react";

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
