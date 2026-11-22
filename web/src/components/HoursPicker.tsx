const STORAGE_KEY = "terramind.dayHours";
const OPTIONS: { value: number | null; label: string }[] = [
  { value: null, label: "רק מסלול" },
  { value: 4, label: "חצי יום" },
  { value: 6, label: "6 שעות" },
  { value: 9, label: "יום שלם" },
];

export function loadDayHours(): number | null {
  try {
    const v = Number(localStorage.getItem(STORAGE_KEY));
    return OPTIONS.some((o) => o.value === v) ? v : null;
  } catch {
    return null;
  }
}

export function saveDayHours(v: number | null): void {
  try {
    if (v == null) localStorage.removeItem(STORAGE_KEY);
    else localStorage.setItem(STORAGE_KEY, String(v));
  } catch {
    // storage blocked: the choice just isn't remembered
  }
}

/**
 * How long the whole day out is (hike + places, not the drive from home). Agent 4 fills that time with
 * places around the hike; a length written in the message ("יום של 5 שעות") wins over this picker.
 */
export function HoursPicker({ value, onChange }: { value: number | null; onChange: (v: number | null) => void }) {
  return (
    <div className="hours" role="radiogroup" aria-label="אורך היום">
      <span className="origin-label">אורך היום:</span>
      {OPTIONS.map((o) => (
        <button
          key={o.label}
          type="button"
          role="radio"
          aria-checked={value === o.value}
          className={`hours-btn${value === o.value ? " on" : ""}`}
          onClick={() => onChange(o.value)}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}
