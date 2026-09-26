import type { RosterPage } from '@/types/api';

/**
 * /api/roster no longer serialises account credentials (signup email,
 * forwarding address, Cloudflare alias/rule/destination): the operator UI
 * sends no credential, so anything it can read, anyone can read. The keys are
 * removed, not nulled, so an absent key means "hidden", while null still
 * means "none". Rows returned by other endpoints may still carry them.
 */
export const ROSTER_HIDDEN_LABEL = 'hidden pending operator auth';
export const ALIAS_REMOVAL_DISABLED = 'Alias removal is disabled pending operator auth';

export function rosterEmailHidden(page: RosterPage): boolean {
  return !('email_alias' in page) && !('signup_email' in page);
}
