export type AdminNavigationMatch = 'exact' | 'none' | 'prefix';

export interface AdminNavigationPathMatch {
  path: string;
  match: Exclude<AdminNavigationMatch, 'none'>;
}

export interface AdminNavigationItem {
  label: string;
  href: string;
  match: AdminNavigationMatch;
  aliases?: AdminNavigationPathMatch[];
}

export interface AdminNavigationGroup {
  label: string;
  items: AdminNavigationItem[];
}

export const ADMIN_NAVIGATION_GROUPS: AdminNavigationGroup[] = [
  {
    label: 'Workspaces',
    items: [
      { label: 'Overview', href: '/admin', match: 'exact' },
      { label: 'Analytics', href: '/admin/analytics', match: 'prefix' },
      { label: 'Recovery', href: '/admin/recovery', match: 'prefix' },
      { label: 'Sources', href: '/admin/sources', match: 'prefix' },
      {
        label: 'Moderation',
        href: '/admin/moderation',
        match: 'prefix',
        aliases: [{ path: '/admin/memes', match: 'prefix' }]
      },
      { label: 'Blocked patterns', href: '/admin/moderation/patterns', match: 'prefix' }
    ]
  },
  {
    label: 'Content',
    items: [
      { label: 'Synonyms', href: '/admin/search/synonyms', match: 'prefix' },
      { label: 'SEO', href: '/admin/content/seo', match: 'prefix' },
      { label: 'Templates', href: '/admin/content/templates', match: 'prefix' }
    ]
  },
  {
    label: 'Accounts',
    items: [{ label: 'Telegram accounts', href: '/admin/telegram', match: 'prefix' }]
  }
];

export const ADMIN_NAVIGATION_ITEMS = ADMIN_NAVIGATION_GROUPS.flatMap((group) => group.items);

export const ADMIN_CATALOG_LINK: AdminNavigationItem = {
  label: 'Back to catalog',
  href: '/',
  match: 'none'
};

function matchesPath(
  pathMatch: AdminNavigationPathMatch,
  currentPath: string
): boolean {
  return pathMatch.match === 'exact'
    ? currentPath === pathMatch.path
    : currentPath === pathMatch.path || currentPath.startsWith(`${pathMatch.path}/`);
}

function activeMatchSpecificity(item: AdminNavigationItem, currentPath: string): number | null {
  if (item.match === 'none') return null;
  const pathMatches: AdminNavigationPathMatch[] = [
    { path: item.href, match: item.match },
    ...(item.aliases ?? [])
  ];
  const matchingPathLengths = pathMatches
    .filter((pathMatch) => matchesPath(pathMatch, currentPath))
    .map((pathMatch) => pathMatch.path.length);
  return matchingPathLengths.length === 0 ? null : Math.max(...matchingPathLengths);
}

export function getActiveAdminNavigationItem(currentPath: string): AdminNavigationItem | null {
  let activeItem: AdminNavigationItem | null = null;
  let highestSpecificity = -1;
  for (const item of ADMIN_NAVIGATION_ITEMS) {
    const specificity = activeMatchSpecificity(item, currentPath);
    if (specificity !== null && specificity > highestSpecificity) {
      activeItem = item;
      highestSpecificity = specificity;
    }
  }
  return activeItem;
}

export function isAdminNavigationItemActive(item: AdminNavigationItem, currentPath: string): boolean {
  return getActiveAdminNavigationItem(currentPath) === item;
}
