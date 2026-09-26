from django.test import SimpleTestCase

from prospecting.domain.website_enrichment import (
    contact_link_candidates,
    extract_contacts,
    extract_json_ld_contacts,
    is_acceptable_email,
)


class WebsiteEnrichmentExtractionTests(SimpleTestCase):
    def test_mailto_and_text_email(self):
        html = '<html><body><a href="mailto:Contato@Empresa.com.br">Email</a> vendas@empresa.com.br</body></html>'
        emails, phones, addresses, _links = extract_contacts(html, page_url="https://empresa.com.br/")
        self.assertIn("contato@empresa.com.br", emails)
        self.assertIn("vendas@empresa.com.br", emails)
        self.assertFalse(phones)
        self.assertFalse(addresses)

    def test_rejects_image_like_email(self):
        self.assertFalse(is_acceptable_email("logo@empresa.png"))

    def test_tel_link_and_text_phone(self):
        html = '<html><body><a href="tel:+551138833322">Ligar</a> Telefone (11) 3883-3322</body></html>'
        _emails, phones, _addresses, _links = extract_contacts(html, page_url="https://empresa.com.br/")
        self.assertTrue(any("3883" in phone for phone in phones))

    def test_json_ld_organization_contacts(self):
        html = """
        <script type="application/ld+json">
        {"@type":"Hospital","email":"compras@hospital.example.com","telephone":"+55 11 4000-1000",
         "address":{"streetAddress":"Av. Principal, 100","addressLocality":"Barueri","addressRegion":"SP","postalCode":"06400","addressCountry":"BR"}}
        </script>
        """
        emails, phones, addresses = extract_json_ld_contacts(html)
        self.assertIn("compras@hospital.example.com", emails)
        self.assertTrue(phones)
        self.assertTrue(addresses)

    def test_contact_page_link_candidate(self):
        html = '<html><body><a href="/contato">Fale conosco</a><a href="https://facebook.com/x">Social</a></body></html>'
        _emails, _phones, _addresses, links = extract_contacts(html, page_url="https://empresa.com.br/")
        candidates = contact_link_candidates(links, base_url="https://empresa.com.br/", max_links=3)
        self.assertEqual(len(candidates), 1)
        self.assertIn("/contato", candidates[0].url)

    def test_duplicate_emails_deduped(self):
        html = "<html><body>contato@empresa.com.br mailto contato@empresa.com.br</body></html>"
        emails, _phones, _addresses, _links = extract_contacts(html, page_url="https://empresa.com.br/")
        self.assertEqual(len(emails), 1)

    def test_address_tag(self):
        html = "<html><body><address>Av. Zélia, 587 - Barueri, SP - CEP 06401-050</address></body></html>"
        _emails, _phones, addresses, _links = extract_contacts(html, page_url="https://empresa.com.br/contato")
        self.assertTrue(addresses)
