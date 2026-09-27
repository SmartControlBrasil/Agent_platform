const CHALLENGE_TERMS = ['captcha', 'unusual traffic', 'not a robot', 'verifique se você não é um robô', 'tráfego incomum'];
const NO_RESULTS_TERMS = ['no results found', 'nenhum resultado encontrado'];

export function detectChallengeHtml(html) {
  const text = stripHtml(html).toLowerCase();
  return CHALLENGE_TERMS.some((term) => text.includes(term));
}

export function classifyGoogleMapsFixture(html) {
  const text = stripHtml(html).toLowerCase();
  if (detectChallengeHtml(html)) return 'challenge';
  if (NO_RESULTS_TERMS.some((term) => text.includes(term))) return 'no_results';
  if (/href=["'][^"']*\/maps\/place\//i.test(html) || /role=["']feed["']/i.test(html)) return 'feed';
  return 'changed_structure';
}

function stripHtml(html) {
  return String(html || '').replace(/<script[\s\S]*?<\/script>/gi, ' ').replace(/<style[\s\S]*?<\/style>/gi, ' ').replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim();
}
