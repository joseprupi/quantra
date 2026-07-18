import { useMemo, useRef } from 'react';

const CACHE_STORAGE_KEY = 'quantra_vol_sample_cache_v1';
const MAX_ENTRIES = 30;

type CacheValue<T> = {
  key: string;
  ts: number;
  value: T;
};

function stableStringify(input: unknown): string {
  if (input === null || typeof input !== 'object') return JSON.stringify(input);
  if (Array.isArray(input)) return `[${input.map(stableStringify).join(',')}]`;
  const obj = input as Record<string, unknown>;
  const keys = Object.keys(obj).sort();
  return `{${keys.map(k => `${JSON.stringify(k)}:${stableStringify(obj[k])}`).join(',')}}`;
}

function loadPersisted<T>(): CacheValue<T>[] {
  try {
    const raw = localStorage.getItem(CACHE_STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter((x: any) => x && typeof x.key === 'string' && x.value !== undefined);
  } catch {
    return [];
  }
}

function persist<T>(entries: CacheValue<T>[]) {
  try {
    localStorage.setItem(CACHE_STORAGE_KEY, JSON.stringify(entries.slice(0, MAX_ENTRIES)));
  } catch {
    // best-effort cache
  }
}

export function useVolSurfaceCache<T>() {
  const memoryRef = useRef<CacheValue<T>[]>(loadPersisted<T>());

  return useMemo(() => {
    const get = (request: unknown): T | null => {
      const key = stableStringify(request);
      const idx = memoryRef.current.findIndex(e => e.key === key);
      if (idx < 0) return null;
      const hit = memoryRef.current[idx];
      // LRU promote
      memoryRef.current.splice(idx, 1);
      memoryRef.current.unshift({ ...hit, ts: Date.now() });
      persist(memoryRef.current);
      return hit.value;
    };

    const set = (request: unknown, value: T): void => {
      const key = stableStringify(request);
      memoryRef.current = memoryRef.current.filter(e => e.key !== key);
      memoryRef.current.unshift({ key, value, ts: Date.now() });
      if (memoryRef.current.length > MAX_ENTRIES) {
        memoryRef.current = memoryRef.current.slice(0, MAX_ENTRIES);
      }
      persist(memoryRef.current);
    };

    const hash = (request: unknown): string => stableStringify(request);

    const clear = (): void => {
      memoryRef.current = [];
      try {
        localStorage.removeItem(CACHE_STORAGE_KEY);
      } catch {
        // best-effort cache
      }
    };

    return { get, set, hash, clear };
  }, []);
}

