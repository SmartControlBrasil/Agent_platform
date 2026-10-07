export const GoogleMapsSelectors = Object.freeze({
  /** Result list only — do not match the map canvas `[role="main"]`. */
  resultsFeed: '[role="feed"]',
  resultAnchors:
    'a[href*="/maps/place/"], a[href*="/maps/search/"], a[href*="/maps?cid="], a[href*="/maps?ll="]',
  placePanel: '[role="main"]',
  websiteLinks: 'a[data-item-id="authority"], a[aria-label^="Website"], a[href^="http"]',
  phoneButtons:
    'button[data-item-id^="phone"], button[aria-label*="Phone"], button[aria-label*="Telefone"]',
  addressButtons:
    'button[data-item-id="address"], button[aria-label*="Address"], button[aria-label*="Endereço"]',
  noResultsText: 'No results found,Nenhum resultado encontrado',
  challengeText:
    'captcha,unusual traffic,not a robot,verifique se você não é um robô,tráfego incomum',
  consentText:
    'before you continue,antes de continuar,accept all,aceitar tudo,rejeitar tudo',
  feedReadyLabels:
    'results for,resultados para,results,resultados,resultado',
});
