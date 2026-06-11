import type { CurrentSessionRead, LinkedProvidersRead } from '$lib/api/types';

export function accountStatusLabel(session: CurrentSessionRead | null): string {
  if (!session) {
    return 'Session unavailable';
  }

  return session.user.account_type === 'full' ? 'Full profile' : 'Guest profile';
}

export function accountBenefitText(session: CurrentSessionRead | null): string {
  if (!session) {
    return 'We will keep browsing public until the account session is reachable.';
  }

  if (session.user.account_type === 'full') {
    return 'Saves and favorites are kept with your connected profile.';
  }

  return 'Connect Telegram to keep saves and favorites across browsers.';
}

export function connectedProviderLabels(providers: LinkedProvidersRead | null): string[] {
  if (!providers) {
    return [];
  }

  const labels: string[] = [];
  if (providers.telegram_linked) {
    labels.push('Telegram');
  }
  if (providers.google_linked) {
    labels.push('Google');
  }
  if (providers.email) {
    labels.push('Email');
  }
  return labels;
}
