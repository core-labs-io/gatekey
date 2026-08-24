"use client";

/**
 * Minimal shared data-fetching hook - the load/error/refetch boilerplate
 * that was previously copy-pasted per page, in one place.
 *
 * Convention for new screens (and for pages as they get touched): reach for
 * this instead of hand-rolling useEffect + three useStates. `refetch` keeps
 * the previous data on screen while reloading (no skeleton flash), matching
 * the dashboard's refresh behavior.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "@/lib/api";

export interface ApiQuery<T> {
  data: T | null;
  loading: boolean;
  error: string | null;
  /** Reload, keeping current data visible while the request runs. */
  refetch: () => void;
}

export function useApiQuery<T>(
  fetcher: () => Promise<T>,
  deps: unknown[] = [],
): ApiQuery<T> {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // Monotonic id so a stale response never overwrites a newer one.
  const requestSeq = useRef(0);

  const run = useCallback(() => {
    const seq = ++requestSeq.current;
    setLoading(true);
    setError(null);
    fetcher()
      .then((result) => {
        if (requestSeq.current !== seq) return;
        setData(result);
      })
      .catch((err) => {
        if (requestSeq.current !== seq) return;
        setError(err instanceof ApiError ? err.message : "Request failed.");
      })
      .finally(() => {
        if (requestSeq.current !== seq) return;
        setLoading(false);
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  useEffect(() => {
    run();
    return () => {
      // Invalidate in-flight responses on unmount/dep change.
      requestSeq.current++;
    };
  }, [run]);

  return { data, loading, error, refetch: run };
}
