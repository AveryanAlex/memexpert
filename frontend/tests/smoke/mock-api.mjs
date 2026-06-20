import { createServer } from 'node:http';

const port = Number(process.env.PORT ?? 8787);
const seededCollectionId = 'smoke-private-team-saves';

const meme = {
  id: 'smoke-meme-1',
  media_type: 'image',
  language: 'en',
  is_nsfw: false,
  popularity_score: 42.5,
  like_count: 7,
  tags: ['cat', 'reaction', 'smoke'],
  primary_file: {
    id: 'smoke-file-1',
    mime_type: 'image/svg+xml',
    width: 640,
    height: 360,
    file_size_bytes: 512,
    blur_hash: null,
    quality_score: 0.99,
    render: {
      thumbnail_url: `http://127.0.0.1:${port}/media/smoke-cat.svg`,
      preview_url: `http://127.0.0.1:${port}/media/smoke-cat.svg`,
      display_url: `http://127.0.0.1:${port}/media/smoke-cat.svg`,
      original_url: `http://127.0.0.1:${port}/media/smoke-cat.svg`,
      download_url: `http://127.0.0.1:${port}/media/smoke-cat.svg`,
      web_video_url: null,
      width: 640,
      height: 360,
      blur_hash: null
    },
    render_url: `http://127.0.0.1:${port}/media/smoke-cat.svg`,
    download_url: `http://127.0.0.1:${port}/media/smoke-cat.svg`
  },
  caption: 'Smoke test cat reaction',
  seo_page_slug: 'smoke-test-cat-reaction',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
  render_url: `http://127.0.0.1:${port}/media/smoke-cat.svg`,
  download_url: `http://127.0.0.1:${port}/media/smoke-cat.svg`,
  viewer_has_favorited: false,
  viewer_has_saved: false,
  viewer_has_pinned: false
};

const detail = {
  ...meme,
  ocr_text: 'cat says ship it',
  seo_title: 'Smoke test cat reaction',
  seo_description: 'A deterministic smoke-test meme served by the local mocked backend.',
  seo_alt_text: 'A cat reaction smoke test graphic',
  seo_body_text: 'Used by Playwright to verify search, detail, media, and action visibility.',
  seo_model_id: null,
  seo_prompt_version: null,
  seo_generated_at: null,
  files: [meme.primary_file]
};

const nextMeme = {
  ...meme,
  id: 'smoke-meme-2',
  caption: 'Smoke test deploy mood',
  seo_page_slug: 'smoke-test-deploy-mood',
  tags: ['deploy', 'smoke'],
  primary_file: {
    ...meme.primary_file,
    id: 'smoke-file-2'
  }
};

const collectionMeme = {
  ...meme,
  id: 'smoke-meme-collection-1',
  caption: 'Smoke test vault reaction',
  seo_page_slug: 'smoke-test-vault-reaction',
  tags: ['vault', 'reaction', 'collection'],
  primary_file: {
    ...meme.primary_file,
    id: 'smoke-file-collection-1'
  },
  viewer_access: { visibility: 'shared' }
};

const seededCollection = {
  id: seededCollectionId,
  owner_id: 'smoke-owner-user',
  title: 'Smoke private team saves',
  description: 'Deterministic collection used by browser smoke tests.',
  kind: 'custom',
  visibility: 'private',
  memberships: [],
  invites: [],
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z'
};

const seededCollectionSummary = {
  collection: seededCollection,
  viewer_role: 'viewer',
  capabilities: {
    can_view: true,
    can_add_memes: false,
    can_remove_memes: false,
    can_rename: false,
    can_delete: false,
    can_create_invites: false,
    can_revoke_invites: false,
    can_manage_members: false,
    can_set_active_save: true
  },
  active_save_collection_id: seededCollectionId
};

const trend = {
  recent: { views: 120, sends: 8, likes: 7, saves: 4, downloads: 3 },
  previous: { views: 90, sends: 5, likes: 4, saves: 2, downloads: 1 },
  latest_snapshot_at: '2026-01-01T00:00:00Z',
  latest_source_views: 120,
  latest_source_reactions: 7,
  latest_source_reposts: 8,
  latest_platform_views: 120,
  latest_platform_sends: 8,
  latest_platform_saves: 4,
  latest_platform_likes: 7,
  latest_popularity_score: 42.5,
  engagement_24h: 22,
  trending_score: 42.5,
  refreshed_at: '2026-01-01T00:00:00Z'
};

const server = createServer((request, response) => {
  const url = new URL(request.url ?? '/', `http://${request.headers.host ?? `127.0.0.1:${port}`}`);

  if (url.pathname === '/health') {
    sendJson(response, 200, { ok: true });
    return;
  }

  if (url.pathname === '/media/smoke-cat.svg') {
    response.writeHead(200, {
      'content-type': 'image/svg+xml',
      'cache-control': 'no-store'
    });
    response.end(svgImage());
    return;
  }

  if (url.pathname === '/api/v1/auth/session') {
    if (hasFullAccess(request)) {
      sendJson(response, 200, sessionPayload('full'));
      return;
    }

    sendJson(response, 401, { detail: 'Smoke test runs as a guest browser.' });
    return;
  }

  if (url.pathname === '/api/v1/auth/telegram-miniapp') {
    readRequestJson(request).then((body) => {
      if (!body || typeof body.initData !== 'string' || !body.initData.includes('smoke-miniapp-init-data')) {
        sendJson(response, 401, { detail: 'Invalid smoke Telegram initData.' });
        return;
      }

      sendJson(response, 200, sessionPayload('full'), {
        'set-cookie': 'memexpert_access_token=miniapp-full; Path=/; HttpOnly; SameSite=Lax'
      });
    }).catch(() => {
      sendJson(response, 400, { detail: 'Invalid JSON body.' });
    });
    return;
  }

  if (url.pathname === '/api/v1/collections') {
    if (!hasFullAccess(request)) {
      sendJson(response, 401, { detail: 'Sign in to load collection choices.' });
      return;
    }

    sendJson(response, 200, {
      collections: [seededCollectionSummary],
      active_save_collection_id: seededCollectionId
    });
    return;
  }

  if (url.pathname === '/api/v1/memes/search') {
    if (isSeededCollectionSearch(url)) {
      if (!hasFullAccess(request)) {
        sendJson(response, 403, { detail: 'Sign in with access to this collection to search it.' });
        return;
      }

      sendJson(response, 200, searchPage([{ meme: collectionMeme, attribution: attributionFor(url, collectionMeme.id) }], url, 'req_smoke_collection'));
      return;
    }

    sendJson(response, 200, {
      items: [{ meme }],
      limit: Number(url.searchParams.get('limit') ?? 12),
      offset: Number(url.searchParams.get('offset') ?? 0),
      total: 1,
      has_more: false
    });
    return;
  }

  if (url.pathname === '/api/v1/memes/home-feed' || url.pathname === '/api/v1/memes/browse') {
    const offset = Number(url.searchParams.get('offset') ?? 0);
    sendJson(response, 200, {
      items: [{ meme: offset > 0 ? nextMeme : meme }],
      limit: Number(url.searchParams.get('limit') ?? 12),
      offset,
      total: 2,
      has_more: offset <= 0
    });
    return;
  }

  if (url.pathname === '/api/v1/memes/slug/smoke-test-cat-reaction') {
    sendJson(response, 200, detail);
    return;
  }

  if (url.pathname === '/api/v1/memes/smoke-meme-1/popularity') {
    sendJson(response, 200, {
      meme_id: 'smoke-meme-1',
      trend,
      sparkline: [
        { captured_at: '2026-01-01T00:00:00Z', source_views: 90, source_reactions: 4, source_reposts: 5, platform_views: 90, platform_sends: 5, platform_saves: 2, platform_likes: 4, popularity_score: 30 },
        { captured_at: '2026-01-01T01:00:00Z', source_views: 120, source_reactions: 7, source_reposts: 8, platform_views: 120, platform_sends: 8, platform_saves: 4, platform_likes: 7, popularity_score: 42.5 }
      ]
    });
    return;
  }

  if (/^\/api\/v1\/memes\/[^/]+\/(?:detail-click|download|impression|share)$/.test(url.pathname)) {
    sendJson(response, 200, { ok: true });
    return;
  }

  sendJson(response, 404, { detail: `Unhandled smoke API route: ${url.pathname}` });
});

server.listen(port, '127.0.0.1', () => {
  process.stdout.write(`Smoke API listening on http://127.0.0.1:${port}\n`);
});

function sendJson(response, status, payload, headers = {}) {
  response.writeHead(status, {
    'content-type': 'application/json',
    'cache-control': 'no-store',
    ...headers
  });
  response.end(JSON.stringify(payload));
}

function readRequestJson(request) {
  return new Promise((resolve, reject) => {
    let body = '';
    request.setEncoding('utf8');
    request.on('data', (chunk) => {
      body += chunk;
    });
    request.on('end', () => {
      try {
        resolve(body ? JSON.parse(body) : null);
      } catch (error) {
        reject(error);
      }
    });
    request.on('error', reject);
  });
}

function hasFullAccess(request) {
  return (request.headers.cookie ?? '').includes('memexpert_access_token=miniapp-full');
}

function isSeededCollectionSearch(url) {
  return url.searchParams.get('scope') === 'collections' && url.searchParams.getAll('collection_ids').includes(seededCollectionId);
}

function searchPage(items, url, requestId) {
  return {
    items,
    limit: Number(url.searchParams.get('limit') ?? 12),
    offset: Number(url.searchParams.get('offset') ?? 0),
    total: items.length,
    has_more: false,
    request_id: requestId
  };
}

function attributionFor(url, memeId) {
  return {
    request_id: 'req_smoke_collection',
    impression_id: `imp_${memeId}`,
    surface: 'search',
    source_algorithm: 'smoke-collection-seed',
    rank: 1,
    query: url.searchParams.get('query') ?? null,
    filters: {
      language: url.searchParams.get('language'),
      media_type: url.searchParams.get('media_type'),
      include_nsfw: url.searchParams.get('include_nsfw') === 'true',
      tags: url.searchParams.getAll('tags'),
      scope: url.searchParams.get('scope'),
      collection_ids: url.searchParams.getAll('collection_ids')
    },
    collection_scope: 'collections',
    collection_ids: url.searchParams.getAll('collection_ids'),
    source_meme_id: null,
    algorithm_version: 'smoke-v1',
    score: 1,
    score_components: { collection_match: 1 },
    reason: 'Seeded readable collection result'
  };
}

function sessionPayload(accountType) {
  return {
    user: {
      id: accountType === 'full' ? 'smoke-full-user' : 'smoke-guest-user',
      account_type: accountType,
      telegram_id: accountType === 'full' ? 303030303 : null,
      google_id: null,
      email: null,
      email_verified_at: null,
      language: 'any',
      nsfw_enabled: false,
      token_nonce: 0,
      status: 'active',
      guest_expires_at: accountType === 'guest' ? '2026-07-12T00:00:00Z' : null,
      active_save_collection_id: null,
      is_admin: false,
      created_at: '2026-01-01T00:00:00Z',
      updated_at: '2026-01-01T00:00:00Z'
    },
    linked_providers: {
      email: null,
      email_verified_at: null,
      has_password: false,
      google_linked: false,
      telegram_linked: accountType === 'full'
    }
  };
}

function svgImage() {
  return `<svg xmlns="http://www.w3.org/2000/svg" width="640" height="360" viewBox="0 0 640 360" role="img" aria-label="Smoke test cat reaction">
  <rect width="640" height="360" fill="#111827"/>
  <circle cx="220" cy="165" r="82" fill="#facc15"/>
  <circle cx="190" cy="145" r="12" fill="#111827"/>
  <circle cx="250" cy="145" r="12" fill="#111827"/>
  <path d="M180 220 Q220 250 260 220" fill="none" stroke="#111827" stroke-width="14" stroke-linecap="round"/>
  <text x="340" y="150" fill="#f9fafb" font-family="Arial, sans-serif" font-size="34" font-weight="700">ship it</text>
  <text x="340" y="200" fill="#93c5fd" font-family="Arial, sans-serif" font-size="24">smoke media</text>
</svg>`;
}
