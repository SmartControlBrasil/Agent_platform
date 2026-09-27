export const GoogleMapsMessages = Object.freeze({
  WAIT_READY: 'GMAPS_WAIT_READY',
  WAIT_DETAIL: 'GMAPS_WAIT_DETAIL',
  COLLECT_FEED: 'GMAPS_COLLECT_FEED',
  OPEN_RESULT: 'GMAPS_OPEN_RESULT',
  EXTRACT_DETAIL: 'GMAPS_EXTRACT_DETAIL',
  SCROLL: 'GMAPS_SCROLL',
  DETECT_CHALLENGE: 'GMAPS_DETECT_CHALLENGE',
});

export const ALLOWED_GOOGLE_MAPS_MESSAGES = new Set(Object.values(GoogleMapsMessages));

export function validateGoogleMapsMessage(message) {
  if (!message || typeof message !== 'object') return false;
  return ALLOWED_GOOGLE_MAPS_MESSAGES.has(message.type);
}
