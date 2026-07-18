import { useEffect, useMemo, useState } from 'react';
import Header from '../../components/Header';
import { CALENDAR_NAMES, type CalendarName } from '../../lib/quantra-types';
import { orchestratorPost } from '../../lib/api/orchestrator';

interface CalendarDatesResponse {
  dates: string[];
  count?: number;
}

function toDateKey(value: string): string {
  return value.replace(/\//g, '-').slice(0, 10);
}

function firstDayOfMonthIso(d: Date): string {
  return new Date(d.getFullYear(), d.getMonth(), 1).toISOString().slice(0, 10);
}

function lastDayOfMonthIso(d: Date): string {
  return new Date(d.getFullYear(), d.getMonth() + 1, 0).toISOString().slice(0, 10);
}

function enumerateDays(startIso: string, endIso: string): string[] {
  // Guard against partial input while typing in date fields.
  const isoDate = /^\d{4}-\d{2}-\d{2}$/;
  if (!isoDate.test(startIso) || !isoDate.test(endIso)) return [];

  const out: string[] = [];
  const start = new Date(`${startIso}T00:00:00Z`);
  const end = new Date(`${endIso}T00:00:00Z`);
  if (isNaN(start.getTime()) || isNaN(end.getTime()) || start > end) return out;

  // Safety cap to prevent UI freezes on accidentally huge ranges.
  const MAX_DAYS = 3700;
  const totalDays = Math.floor((end.getTime() - start.getTime()) / 86400000) + 1;
  if (totalDays > MAX_DAYS) return out;

  const cursor = new Date(start);
  while (cursor <= end) {
    out.push(cursor.toISOString().slice(0, 10));
    cursor.setUTCDate(cursor.getUTCDate() + 1);
  }
  return out;
}

function monthLabel(d: Date): string {
  return d.toLocaleString('default', { month: 'long', year: 'numeric' });
}

function buildMonthWeeks(year: number, monthIndex: number): Array<Array<number | null>> {
  const first = new Date(year, monthIndex, 1);
  const last = new Date(year, monthIndex + 1, 0);
  const startDay = first.getDay();
  const daysInMonth = last.getDate();
  const weeks: Array<Array<number | null>> = [];
  let current = 1;
  for (let w = 0; w < 6; w++) {
    const week: Array<number | null> = [];
    for (let d = 0; d < 7; d++) {
      if (w === 0 && d < startDay) week.push(null);
      else if (current > daysInMonth) week.push(null);
      else week.push(current++);
    }
    weeks.push(week);
  }
  return weeks;
}

function downloadCsv(filename: string, headers: string[], rows: string[][]): void {
  const esc = (v: string) => `"${v.replace(/"/g, '""')}"`;
  const csv = [headers.map(esc).join(','), ...rows.map((r) => r.map(esc).join(','))].join('\n');
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

export default function CalendarApp() {
  const now = new Date();
  const [calendar, setCalendar] = useState<CalendarName>('TARGET');
  const [monthCursor, setMonthCursor] = useState<Date>(new Date(now.getFullYear(), now.getMonth(), 1));
  const [monthBusinessDays, setMonthBusinessDays] = useState<string[]>([]);
  const [monthHolidays, setMonthHolidays] = useState<string[]>([]);
  const [monthLoading, setMonthLoading] = useState(false);
  const [monthError, setMonthError] = useState<string | null>(null);

  const [startDate, setStartDate] = useState<string>(firstDayOfMonthIso(now));
  const [endDate, setEndDate] = useState<string>(lastDayOfMonthIso(now));
  const [includeWeekends, setIncludeWeekends] = useState(false);
  const [loadingRange, setLoadingRange] = useState(false);
  const [rangeError, setRangeError] = useState<string | null>(null);
  const [businessDays, setBusinessDays] = useState<string[]>([]);
  const [holidays, setHolidays] = useState<string[]>([]);

  const allDays = useMemo(() => enumerateDays(startDate, endDate), [startDate, endDate]);
  const monthWeeks = useMemo(
    () => buildMonthWeeks(monthCursor.getFullYear(), monthCursor.getMonth()),
    [monthCursor]
  );
  const monthBusinessSet = useMemo(() => new Set(monthBusinessDays.map(toDateKey)), [monthBusinessDays]);
  const monthHolidaySet = useMemo(() => new Set(monthHolidays.map(toDateKey)), [monthHolidays]);
  const businessSet = useMemo(() => new Set(businessDays.map(toDateKey)), [businessDays]);
  const holidaySet = useMemo(() => new Set(holidays.map(toDateKey)), [holidays]);

  useEffect(() => {
    let cancelled = false;
    async function loadMonthData() {
      setMonthLoading(true);
      setMonthError(null);
      const monthStart = firstDayOfMonthIso(monthCursor);
      const monthEnd = lastDayOfMonthIso(monthCursor);
      try {
        const [bd, hd] = await Promise.all([
          orchestratorPost<CalendarDatesResponse>('/v1/calendar/business-days', {
            calendar,
            start_date: monthStart,
            end_date: monthEnd,
            include_start: true,
            include_end: true,
          }),
          orchestratorPost<CalendarDatesResponse>('/v1/calendar/holidays', {
            calendar,
            start_date: monthStart,
            end_date: monthEnd,
            include_weekends: includeWeekends,
          }),
        ]);
        if (!bd.ok) throw new Error(bd.envelope.error || 'Failed loading month business days');
        if (!hd.ok) throw new Error(hd.envelope.error || 'Failed loading month holidays');
        if (cancelled) return;
        setMonthBusinessDays((bd.data?.dates || []).map(toDateKey));
        setMonthHolidays((hd.data?.dates || []).map(toDateKey));
      } catch (err) {
        if (cancelled) return;
        setMonthError(err instanceof Error ? err.message : 'Month request failed');
        setMonthBusinessDays([]);
        setMonthHolidays([]);
      } finally {
        if (!cancelled) setMonthLoading(false);
      }
    }
    loadMonthData();
    return () => {
      cancelled = true;
    };
  }, [calendar, monthCursor, includeWeekends]);

  async function loadRangeData(overrideIncludeWeekends?: boolean) {
    setLoadingRange(true);
    setRangeError(null);
    try {
      const includeWeekendsValue =
        typeof overrideIncludeWeekends === 'boolean' ? overrideIncludeWeekends : includeWeekends;
      const [bd, hd] = await Promise.all([
        orchestratorPost<CalendarDatesResponse>('/v1/calendar/business-days', {
          calendar,
          start_date: startDate,
          end_date: endDate,
          include_start: true,
          include_end: true,
        }),
        orchestratorPost<CalendarDatesResponse>('/v1/calendar/holidays', {
          calendar,
          start_date: startDate,
          end_date: endDate,
          include_weekends: includeWeekendsValue,
        }),
      ]);

      if (!bd.ok) throw new Error(bd.envelope.error || 'Failed loading business days');
      if (!hd.ok) throw new Error(hd.envelope.error || 'Failed loading holidays');

      setBusinessDays((bd.data?.dates || []).map(toDateKey));
      setHolidays((hd.data?.dates || []).map(toDateKey));
    } catch (err) {
      setRangeError(err instanceof Error ? err.message : 'Range request failed');
    } finally {
      setLoadingRange(false);
    }
  }

  function prevMonth() {
    setMonthCursor((m) => new Date(m.getFullYear(), m.getMonth() - 1, 1));
  }

  function nextMonth() {
    setMonthCursor((m) => new Date(m.getFullYear(), m.getMonth() + 1, 1));
  }

  function monthDate(day: number): string {
    return new Date(monthCursor.getFullYear(), monthCursor.getMonth(), day).toISOString().slice(0, 10);
  }

  function exportBusinessCsv() {
    downloadCsv('calendar-business-days.csv', ['date'], businessDays.map((d) => [d]));
  }

  function exportHolidaysCsv() {
    downloadCsv('calendar-holidays.csv', ['date'], holidays.map((d) => [d]));
  }

  function exportCombinedCsv() {
    const byDate = new Set([...businessDays, ...holidays]);
    const rows = Array.from(byDate)
      .sort((a, b) => a.localeCompare(b))
      .map((date) => [date, businessSet.has(date) ? 'true' : 'false', holidaySet.has(date) ? 'true' : 'false']);
    downloadCsv('calendar-combined.csv', ['date', 'is_business_day', 'is_holiday'], rows);
  }

  return (
    <div className="min-h-screen bg-[#fafafa]">
      <Header />
      <main className="max-w-6xl mx-auto px-4 sm:px-6 pt-24 pb-12">
        <div className="mb-6">
          <h1 className="text-2xl font-semibold text-[#0a0a0a]">Calendar</h1>
          <p className="text-[#737373] mt-1">Month calendar + range tools for business days and holidays.</p>
        </div>

        {/* Month calendar block */}
        <div className="bg-white border border-[#e5e5e5] rounded-xl p-4 mb-4">
          <div className="grid sm:grid-cols-2 lg:grid-cols-4 gap-3 items-end mb-3">
            <div>
              <label className="block text-xs text-[#737373] mb-1">Calendar</label>
              <select
                value={calendar}
                onChange={(e) => setCalendar(e.target.value as CalendarName)}
                className="w-full px-3 py-2 text-sm border border-[#d4d4d4] rounded-lg"
              >
                {CALENDAR_NAMES.map((name) => (
                  <option key={name} value={name}>
                    {name}
                  </option>
                ))}
              </select>
            </div>
            <div className="flex items-center gap-2">
              <button
                onClick={prevMonth}
                className="w-14 px-2 py-2 text-xs font-medium text-[#525252] bg-white border border-[#d4d4d4] rounded-lg hover:bg-[#f5f5f5]"
              >
                Prev
              </button>
              <span className="text-sm text-[#525252] text-center min-w-[160px] tabular-nums">{monthLabel(monthCursor)}</span>
              <button
                onClick={nextMonth}
                className="w-14 px-2 py-2 text-xs font-medium text-[#525252] bg-white border border-[#d4d4d4] rounded-lg hover:bg-[#f5f5f5]"
              >
                Next
              </button>
            </div>
            <label className="flex items-center gap-2 text-sm text-[#525252]">
              <input
                type="checkbox"
                checked={includeWeekends}
                onChange={(e) => setIncludeWeekends(e.target.checked)}
                className="rounded border-[#d4d4d4]"
              />
              Include weekends in holidays
            </label>
            <div className="text-xs text-[#737373]">
              {monthLoading ? 'Loading month data...' : `Business ${monthBusinessDays.length} · Holidays ${monthHolidays.length}`}
            </div>
            {monthError ? <div className="text-xs text-red-600">{monthError}</div> : <div />}
          </div>

          <div className="grid grid-cols-7 gap-1 text-[10px] text-[#737373] mb-1">
            {['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'].map((d) => (
              <div key={d} className="text-center">{d}</div>
            ))}
          </div>
          <div className="grid grid-cols-7 gap-1">
            {monthWeeks.flat().map((day, idx) => {
              if (!day) return <div key={idx} className="h-8" />;
              const dateKey = monthDate(day);
              const weekday = new Date(`${dateKey}T00:00:00`).getDay();
              const isWeekend = weekday === 0 || weekday === 6;
              const isBusiness = monthBusinessSet.has(dateKey);
              const isHoliday = monthHolidaySet.has(dateKey);
              const cls = isHoliday
                ? 'bg-red-50 text-red-700 border-red-200'
                : isBusiness
                  ? 'bg-green-50 text-green-700 border-green-200'
                  : isWeekend
                    ? 'bg-[#f5f5f5] text-[#a3a3a3] border-[#e5e5e5]'
                    : 'bg-[#fafafa] text-[#737373] border-[#e5e5e5]';
              return (
                <div key={idx} className={`h-8 rounded border text-xs font-semibold flex items-center justify-center ${cls}`} title={dateKey}>
                  {day}
                </div>
              );
            })}
          </div>
          <div className="flex items-center gap-4 mt-3 text-xs text-[#737373]">
            <span className="flex items-center gap-1"><span className="inline-block w-3 h-3 rounded bg-green-50 border border-green-200" />Business day</span>
            <span className="flex items-center gap-1"><span className="inline-block w-3 h-3 rounded bg-red-50 border border-red-200" />Holiday</span>
            <span className="flex items-center gap-1"><span className="inline-block w-3 h-3 rounded bg-[#f5f5f5] border border-[#e5e5e5]" />Weekend</span>
          </div>
        </div>

        {/* Range tools block */}
        <div className="bg-white border border-[#e5e5e5] rounded-xl p-4 mb-4">
          <div className="text-sm font-semibold text-[#0a0a0a] mb-3">Range lists and export</div>
          <div className="grid sm:grid-cols-2 lg:grid-cols-5 gap-3 items-end">
            <div>
              <label className="block text-xs text-[#737373] mb-1">Start date</label>
              <input
                type="date"
                value={startDate}
                onChange={(e) => setStartDate(e.target.value)}
                className="w-full px-3 py-2 text-sm border border-[#d4d4d4] rounded-lg"
              />
            </div>
            <div>
              <label className="block text-xs text-[#737373] mb-1">End date</label>
              <input
                type="date"
                value={endDate}
                onChange={(e) => setEndDate(e.target.value)}
                className="w-full px-3 py-2 text-sm border border-[#d4d4d4] rounded-lg"
              />
            </div>
            <button
              onClick={() => loadRangeData()}
              disabled={loadingRange}
              className="px-4 py-2 text-sm font-medium text-white bg-[#0a0a0a] rounded-lg hover:bg-[#262626] disabled:opacity-60"
            >
              {loadingRange ? 'Loading...' : 'Load Range'}
            </button>
            <div className="text-xs text-[#737373] text-right">
              Days {allDays.length} · Business {businessDays.length} · Holidays {holidays.length}
            </div>
          </div>
          {rangeError && <p className="mt-3 text-sm text-red-600">{rangeError}</p>}
          <div className="flex flex-wrap gap-2 mt-3">
            <button onClick={exportBusinessCsv} className="px-3 py-1.5 text-xs font-medium text-[#525252] bg-white border border-[#d4d4d4] rounded-lg hover:bg-[#f5f5f5]">Download business CSV</button>
            <button onClick={exportHolidaysCsv} className="px-3 py-1.5 text-xs font-medium text-[#525252] bg-white border border-[#d4d4d4] rounded-lg hover:bg-[#f5f5f5]">Download holidays CSV</button>
            <button onClick={exportCombinedCsv} className="px-3 py-1.5 text-xs font-medium text-[#525252] bg-white border border-[#d4d4d4] rounded-lg hover:bg-[#f5f5f5]">Download combined CSV</button>
          </div>
        </div>

        <div className="grid md:grid-cols-2 gap-4">
          <div className="bg-white border border-[#e5e5e5] rounded-xl p-4">
            <div className="text-sm font-semibold text-[#0a0a0a] mb-2">Business days</div>
            <div className="max-h-72 overflow-auto text-xs text-[#525252] space-y-1">
              {businessDays.map((d) => (
                <div key={d}>{d}</div>
              ))}
            </div>
          </div>
          <div className="bg-white border border-[#e5e5e5] rounded-xl p-4">
            <div className="text-sm font-semibold text-[#0a0a0a] mb-2">Holidays / vacations</div>
            <div className="max-h-72 overflow-auto text-xs text-[#525252] space-y-1">
              {holidays.map((d) => (
                <div key={d}>{d}</div>
              ))}
            </div>
          </div>
        </div>
      </main>
    </div>
  );
}
