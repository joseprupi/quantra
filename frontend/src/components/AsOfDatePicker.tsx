// Global As-Of Date picker — sits in the header, controls pricing date across the portal
import { useAsOfDate } from '../hooks/useAsOfDate';

export default function AsOfDatePicker() {
  const { asOfDate, setAsOfDate } = useAsOfDate();

  const handleToday = () => {
    setAsOfDate(new Date().toISOString().split('T')[0]);
  };

  const isToday = asOfDate === new Date().toISOString().split('T')[0];

  return (
    <div className="flex items-center gap-1.5">
      <label className="text-xs text-[#737373] font-medium whitespace-nowrap" title="Global as-of / valuation date for all pricing">
        As-Of
      </label>
      <input
        type="date"
        value={asOfDate}
        onChange={e => setAsOfDate(e.target.value)}
        className={`px-2 py-1 text-xs border rounded-md bg-white focus:outline-none focus:border-[#8a6a2f] transition-colors ${
          isToday ? 'border-[#d4d4d4] text-[#0a0a0a]' : 'border-[#8a6a2f] text-[#8a6a2f] font-medium'
        }`}
        title="Global as-of date — used for pricing and quote resolution"
      />
      {!isToday && (
        <button
          onClick={handleToday}
          className="text-xs text-[#8a6a2f] hover:text-[#6b5324] font-medium"
          title="Reset to today"
        >
          Today
        </button>
      )}
    </div>
  );
}
