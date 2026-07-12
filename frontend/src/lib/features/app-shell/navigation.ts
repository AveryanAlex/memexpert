export interface NavItem {
  label: string;
  href: string;
  match: 'exact' | 'prefix';
  icon: 'discover' | 'search' | 'saved' | 'account' | 'admin';
}

export const PRIMARY_NAV_ITEMS: NavItem[] = [
  { label: 'Discover', href: '/', match: 'exact', icon: 'discover' },
  { label: 'Search', href: '/search', match: 'prefix', icon: 'search' },
  { label: 'Saved', href: '/library', match: 'prefix', icon: 'saved' },
  { label: 'Account', href: '/profile', match: 'prefix', icon: 'account' }
];

export const ADMIN_NAV_ITEM: NavItem = { label: 'Admin', href: '/admin', match: 'prefix', icon: 'admin' };

export function isNavItemActive(item: NavItem, currentPath: string): boolean {
  if (item.href === '/library' && (currentPath === '/collection' || currentPath.startsWith('/collection/'))) return true;
  return item.match === 'exact' ? currentPath === item.href : currentPath === item.href || currentPath.startsWith(`${item.href}/`);
}
