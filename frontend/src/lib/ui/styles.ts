export function cn(...values: unknown[]): string {
  return values.filter((value): value is string => typeof value === 'string' && value.length > 0).join(' ');
}

export const focusRing = 'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent';
export const fieldClass =
  'min-w-0 rounded-xl border border-line bg-paper px-3 py-2.5 text-ink placeholder:text-muted/75 disabled:cursor-not-allowed disabled:opacity-60';
export const panelClass = 'rounded-xl border border-line bg-paper p-5';
export const mutedText = 'text-muted';
