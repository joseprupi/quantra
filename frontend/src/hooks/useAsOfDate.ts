// Hook for global as-of date — subscribes to changes across the portal
import { useState, useEffect, useCallback } from 'react';
import { getAsOfDate, setAsOfDate as setAsOfDateStorage } from '../lib/asOfDate';

export function useAsOfDate() {
  const [asOfDate, setAsOfDateState] = useState(getAsOfDate);

  const setAsOfDate = useCallback((date: string) => {
    setAsOfDateStorage(date);
    setAsOfDateState(date);
  }, []);

  useEffect(() => {
    const handler = (e: Event) => {
      const detail = (e as CustomEvent).detail;
      if (typeof detail === 'string') {
        setAsOfDateState(detail);
      }
    };
    window.addEventListener('quantra-asofdate-change', handler);
    return () => window.removeEventListener('quantra-asofdate-change', handler);
  }, []);

  return { asOfDate, setAsOfDate };
}
