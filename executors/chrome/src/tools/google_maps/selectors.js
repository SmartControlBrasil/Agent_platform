export const GoogleMapsSelectors = Object.freeze({
  feed: '[role="feed"], div[aria-label][role="main"]',
  resultAnchors: 'a[href*="/maps/place/"], a[href*="/maps/search/"]',
  placePanel: '[role="main"]',
  websiteLinks: 'a[data-item-id="authority"], a[aria-label^="Website"], a[href^="http"]',
  phoneButtons: 'button[data-item-id^="phone"], button[aria-label*="Phone"], button[aria-label*="Telefone"]',
  addressButtons: 'button[data-item-id="address"], button[aria-label*="Address"], button[aria-label*="Endereço"]',
  category: 'button[jsaction][aria-label], div[role="main"] button',
  noResultsText: 'No results found,Nenhum resultado encontrado',
  challengeText: 'captcha,unusual traffic,not a robot,verifique se você não é um robô,tráfego incomum',
});
