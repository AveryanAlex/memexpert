import { env } from '$env/dynamic/private';
import type { Actions, PageServerLoad } from './$types';
import { ApiError, fetchAdminSeoReviewRows } from '$lib/api/client';
import { seoActions } from '$lib/server/admin/seoActions';
import { MAX_SEO_REVIEW_PAGE, SEO_REVIEW_PAGE_SIZE, seoReviewPageFromSearchParam } from '$lib/server/admin/seoPagination';

export const load: PageServerLoad = async ({ fetch, request, url }) => {
  const page = seoReviewPageFromSearchParam(url.searchParams.get('page'));
  const offset = (page - 1) * SEO_REVIEW_PAGE_SIZE;
  const paging = { page, pageSize: SEO_REVIEW_PAGE_SIZE, hasPrevious: page > 1, hasNext: false };

  try {
    const rows = await fetchAdminSeoReviewRows(
      {
        fetch,
        baseUrl: env.API_BASE_URL || 'http://localhost:8000',
        cookieHeader: request.headers.get('cookie') ?? undefined
      },
      { limit: SEO_REVIEW_PAGE_SIZE + 1, offset }
    );
    return {
      reviews: rows.slice(0, SEO_REVIEW_PAGE_SIZE),
      paging: { ...paging, hasNext: page < MAX_SEO_REVIEW_PAGE && rows.length > SEO_REVIEW_PAGE_SIZE },
      loadError: null
    };
  } catch (caught) {
    return {
      reviews: [],
      paging,
      loadError: caught instanceof ApiError ? caught.message : 'Could not load the SEO review queue.'
    };
  }
};

export const actions: Actions = seoActions;
