import { redirect } from '@sveltejs/kit';
import type { PageServerLoad } from './$types';

export const load: PageServerLoad = async ({ parent, url }) => {
  const layoutData = await parent();
  const returnTo = sanitizeReturnTo(url.searchParams.get('returnTo'));

  if (layoutData.session?.linked_providers.telegram_linked) {
    throw redirect(303, appendAccountConnected(returnTo));
  }

  return {
    accountType: layoutData.session?.user.account_type ?? null,
    returnTo,
    sessionError: layoutData.sessionError
  };
};

function sanitizeReturnTo(rawReturnTo: string | null): string {
  if (!rawReturnTo?.startsWith('/')) {
    return '/profile';
  }

  if (rawReturnTo.startsWith('//')) {
    return '/profile';
  }

  return rawReturnTo;
}

function appendAccountConnected(returnTo: string): string {
  const [path, query = ''] = returnTo.split('?', 2);
  const params = new URLSearchParams(query);
  params.set('account', 'telegram-connected');
  return `${path}?${params.toString()}`;
}
