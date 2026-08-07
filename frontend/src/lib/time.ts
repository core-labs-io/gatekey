/** Tiny HH:MM <-> HH:MM:SS helpers shared by the Phase 3 rotation/access-
 * schedule forms - backend `time` fields serialize as "HH:MM:SS", the
 * native `<input type="time">` control speaks "HH:MM". */

export function timeToInputValue(value: string | null): string {
  if (!value) return "";
  return value.slice(0, 5);
}

export function inputValueToTime(value: string): string | null {
  if (!value) return null;
  return `${value}:00`;
}
