export function cn(...values: unknown[]): string {
  return values.filter((value): value is string => typeof value === 'string' && value.length > 0).join(' ');
}

export const focusRing = 'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink';
export const fieldClass =
  'min-w-0 rounded-2xl border border-line bg-paper px-4 py-3 text-ink placeholder:text-muted/75 disabled:cursor-not-allowed disabled:opacity-60';
export const panelClass = 'rounded-[28px] border border-line bg-paper p-6 shadow-warm';
export const mutedText = 'text-muted';
