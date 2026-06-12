import { fail, redirect } from '@sveltejs/kit';
import type { Actions, PageServerLoad } from './$types';
import {
  ApiError,
  createCollectionInvite,
  deleteCollection,
  fetchCollectionDetail,
  removeMemeFromCollection,
  setActiveSaveCollection,
  updateCollection,
  type CollectionFormPayload,
  type CollectionInvitePayload
} from '$lib/api/client';
import { apiBaseUrl, forwardBackendAccessCookie } from '$lib/server/backend';

export const load: PageServerLoad = async ({ cookies, fetch, params, request }) => {
  try {
    const detail = await fetchCollectionDetail({
      fetch,
      baseUrl: apiBaseUrl(),
      collectionId: params.id,
      cookieHeader: request.headers.get('cookie') ?? undefined,
      onResponse: (response) => {
        forwardBackendAccessCookie(response, cookies);
      }
    });

    return { detail, errorMessage: null };
  } catch (error) {
    return {
      detail: null,
      errorMessage: error instanceof ApiError ? error.message : 'Could not load this collection.'
    };
  }
};

export const actions: Actions = {
  update: async ({ cookies, fetch, params, request }) => {
    const form = await request.formData();
    try {
      await updateCollection({
        fetch,
        baseUrl: apiBaseUrl(),
        collectionId: params.id,
        cookieHeader: request.headers.get('cookie') ?? undefined,
        body: collectionPayload(form),
        onResponse: (response) => {
          forwardBackendAccessCookie(response, cookies);
        }
      });
    } catch (error) {
      return fail(readStatus(error), { errorMessage: readErrorMessage(error) });
    }

    return { successMessage: 'Collection updated.' };
  },
  delete: async ({ cookies, fetch, params, request }) => {
    try {
      await deleteCollection({
        fetch,
        baseUrl: apiBaseUrl(),
        collectionId: params.id,
        cookieHeader: request.headers.get('cookie') ?? undefined,
        onResponse: (response) => {
          forwardBackendAccessCookie(response, cookies);
        }
      });
    } catch (error) {
      return fail(readStatus(error), { errorMessage: readErrorMessage(error) });
    }

    redirect(303, '/');
  },
  setActive: async ({ cookies, fetch, params, request }) => {
    try {
      await setActiveSaveCollection({
        fetch,
        baseUrl: apiBaseUrl(),
        collectionId: params.id,
        cookieHeader: request.headers.get('cookie') ?? undefined,
        onResponse: (response) => {
          forwardBackendAccessCookie(response, cookies);
        }
      });
    } catch (error) {
      return fail(readStatus(error), { errorMessage: readErrorMessage(error) });
    }

    return { successMessage: 'Active save collection updated.' };
  },
  removeMeme: async ({ cookies, fetch, params, request }) => {
    const form = await request.formData();
    const memeId = String(form.get('meme_id') ?? '').trim();
    if (!memeId) {
      return fail(400, { errorMessage: 'Missing meme id.' });
    }

    try {
      await removeMemeFromCollection({
        fetch,
        baseUrl: apiBaseUrl(),
        collectionId: params.id,
        memeId,
        cookieHeader: request.headers.get('cookie') ?? undefined,
        onResponse: (response) => {
          forwardBackendAccessCookie(response, cookies);
        }
      });
    } catch (error) {
      return fail(readStatus(error), { errorMessage: readErrorMessage(error) });
    }

    return { successMessage: 'Meme removed from collection.' };
  },
  createInvite: async ({ cookies, fetch, params, request, url }) => {
    const form = await request.formData();
    try {
      const invite = await createCollectionInvite({
        fetch,
        baseUrl: apiBaseUrl(),
        collectionId: params.id,
        cookieHeader: request.headers.get('cookie') ?? undefined,
        body: invitePayload(form),
        onResponse: (response) => {
          forwardBackendAccessCookie(response, cookies);
        }
      });
      return {
        successMessage: 'Invite link created.',
        inviteUrl: new URL(invite.join_path, url.origin).toString()
      };
    } catch (error) {
      return fail(readStatus(error), { errorMessage: readErrorMessage(error) });
    }
  }
};

function collectionPayload(form: FormData): CollectionFormPayload {
  return {
    title: String(form.get('title') ?? ''),
    description: String(form.get('description') ?? ''),
    visibility: form.get('visibility') === 'unlisted' ? 'unlisted' : 'private'
  };
}

function invitePayload(form: FormData): CollectionInvitePayload {
  const maxUses = Number.parseInt(String(form.get('max_uses') ?? ''), 10);
  const expiresInHours = Number.parseInt(String(form.get('expires_in_hours') ?? ''), 10);
  return {
    role: form.get('role') === 'editor' ? 'editor' : 'viewer',
    label: String(form.get('label') ?? ''),
    max_uses: Number.isFinite(maxUses) && maxUses > 0 ? maxUses : null,
    expires_in_hours: Number.isFinite(expiresInHours) && expiresInHours > 0 ? expiresInHours : null
  };
}

function readStatus(error: unknown): 400 | 403 | 404 | 409 | 500 {
  if (error instanceof ApiError && [400, 403, 404, 409].includes(error.status)) {
    return error.status as 400 | 403 | 404 | 409;
  }
  return 500;
}

function readErrorMessage(error: unknown): string {
  return error instanceof Error ? error.message : 'Collection action failed.';
}
