import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from gazeta import property_monitor as pm


class PropertyTests(unittest.TestCase):
    def setUp(self):
        self.record = {'url': 'https://example.com/project', 'name': 'ЖК',
                       'text': 'Старт продаж у озера. Цена 7 100 000 рублей.',
                       'hash': 'current', 'kind': 'page', 'previous_text': ''}
        self.card = {'source_id': 0, 'title': 'ЖК <Озеро>', 'category': 'ЖК',
                     'price_rub': 7_100_000, 'price_evidence': 'Цена 7 100 000 рублей.',
                     'facts': 'Цена 7 100 000 рублей.', 'assessment': 'Интересен вид.',
                     'checks': 'Проверить этаж.', 'evidence': 'Старт продаж у озера.'}

    def test_grounding_and_escape(self):
        card = pm.validate_card(self.card, [self.record])
        self.assertIn('&lt;Озеро&gt;', pm.format_card(card))
        for changed in [{'source_id': 9}, {'evidence': 'Вымышленный старт продаж.'},
                        {'facts': 'Цена 999 рублей.'}, {'checks': 'https://evil.example'}]:
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                pm.validate_card(dict(self.card, **changed), [self.record])

    def test_budget_limits(self):
        for category, price, accepted in [
            ('ЖК', 3_000_000, True), ('ЖК', 10_000_000, True),
            ('ЖК', 2_999_999, False), ('ЖК', 10_000_001, False),
            ('Земля', 5_000_000, True), ('Земля', 5_000_001, False),
            ('Земля', 0, False), ('ЖК', None, False)]:
            record = dict(self.record, text=f'Старт продаж у озера. Цена {price} рублей.')
            card = dict(self.card, category=category, price_rub=price,
                        price_evidence=f'Цена {price} рублей.', facts=f'Цена {price} рублей.')
            with self.subTest(category=category, price=price):
                if accepted:
                    pm.validate_card(card, [record])
                else:
                    with self.assertRaises(ValueError):
                        pm.validate_card(card, [record])

    def test_parser_skips_script_and_keeps_lot_price_order(self):
        text, links = pm.page_text('<script>secret()</script><h1>Лот 1</h1>'
                                  '<p>7 100 000</p><a href="/lot/1">Подробнее</a>')
        self.assertNotIn('secret', text)
        self.assertLess(text.index('Лот 1'), text.index('7 100'))
        self.assertEqual(links, ['/lot/1'])

    def test_dry_run_does_not_send_or_save(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(pm, 'STATE', Path(temp)/'state.json'), \
                patch.object(pm, 'collect', return_value=([self.record], [])), \
                patch.object(pm, 'select_cards', return_value=[pm.validate_card(self.card, [self.record])]), \
                patch.object(pm, 'send') as sender:
            pm.run(dry_run=True)
            sender.assert_not_called()
            self.assertFalse(pm.STATE.exists())

    def test_sent_cards_survive_partial_failure_and_retry(self):
        one = pm.validate_card(self.card, [self.record])
        two = dict(one, id='second-card')
        with tempfile.TemporaryDirectory() as temp, patch.object(pm, 'STATE', Path(temp)/'state.json'), \
                patch.object(pm, 'collect', return_value=([self.record], [])), \
                patch.object(pm, 'select_cards', return_value=[one, two]), \
                patch.object(pm, 'send', side_effect=[None, RuntimeError('fail')]):
            with self.assertRaises(RuntimeError):
                pm.run()
            self.assertEqual(json.loads(pm.STATE.read_text())['sent'], [one['id']])
            with patch.object(pm, 'send') as sender:
                pm.run()
                self.assertEqual(sender.call_count, 1)
            with patch.object(pm, 'send') as sender, patch.object(pm, 'select_cards') as editor:
                pm.run()
                sender.assert_not_called()
                editor.assert_not_called()

    def test_all_sources_down_is_failure(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(pm, 'STATE', Path(temp)/'state.json'), \
                patch.object(pm, 'collect', return_value=([], ['offline'])):
            with self.assertRaises(RuntimeError):
                pm.run(dry_run=True)

    def test_invalid_editor_card_does_not_block_valid_card(self):
        from unittest.mock import MagicMock
        invalid = dict(self.card, facts='Цена 999 рублей.')
        answer = json.dumps({'cards': [invalid, self.card]})
        response = MagicMock()
        response.__enter__.return_value = iter([
            ('data: ' + json.dumps({'choices': [{'delta': {'content': answer}}]}) + '\n').encode(),
            b'data: [DONE]\n'])
        with patch.dict(pm.os.environ, {'ZAI_API_KEY': 'test-only'}), \
                patch.object(pm, 'urlopen', return_value=response):
            cards = pm.select_cards([self.record])
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]['facts'], self.card['facts'])
        self.assertTrue(self.record['review_failed'])


if __name__ == '__main__':
    unittest.main()
