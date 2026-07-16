import type { Actions, PageServerLoad } from './$types';
import {
  ApiError,
  fetchAdminSearchSynonymCatalog,
  fetchAdminSearchSynonymSyncState
} from '$lib/api/client';
import { apiRequest } from '$lib/server/admin/actionUtils';
import { searchSynonymActions } from '$lib/server/admin/searchSynonymActions';

export const load: PageServerLoad = async ({ fetch, request }) => {
  const api = apiRequest(fetch, request);
  const [catalogsResult, syncResult] = await Promise.allSettled([
    Promise.all([
      fetchAdminSearchSynonymCatalog(api, 'en'),
      fetchAdminSearchSynonymCatalog(api, 'ru')
    ]).then(([en, ru]) => ({ en, ru })),
    fetchAdminSearchSynonymSyncState(api)
  ]);
  const errors: string[] = [];
  if (catalogsResult.status === 'rejected') {
    errors.push(loadErrorMessage(catalogsResult.reason, 'Could not load synonym catalogs.'));
  }
  if (syncResult.status === 'rejected') {
    errors.push(loadErrorMessage(syncResult.reason, 'Could not load synonym sync state.'));
  }
  return {
    catalogs: catalogsResult.status === 'fulfilled' ? catalogsResult.value : null,
    sync: syncResult.status === 'fulfilled' ? syncResult.value : null,
    requestIds: requestIds(),
    loadedAt: new Date().toISOString(),
    loadError: errors.length ? [...new Set(errors)].join(' ') : null
  };
};

function loadErrorMessage(caught: unknown, fallback: string): string {
  return caught instanceof ApiError ? caught.message : fallback;
}

function requestIds() {
  return {
    en: {
      save: crypto.randomUUID(),
      importSeed: crypto.randomUUID(),
      publish: crypto.randomUUID(),
      reset: crypto.randomUUID()
    },
    ru: {
      save: crypto.randomUUID(),
      importSeed: crypto.randomUUID(),
      publish: crypto.randomUUID(),
      reset: crypto.randomUUID()
    },
    retrySync: crypto.randomUUID()
  };
}

export const actions: Actions = searchSynonymActions;
