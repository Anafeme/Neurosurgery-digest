import os
import unittest
from unittest.mock import Mock, patch

import requests
import main


class DigestTests(unittest.TestCase):
    def test_topic_balance_and_deduplication(self):
        with patch.object(main, 'pubmed_ids', return_value=['1']), patch.object(
            main, 'fetch_articles', side_effect=[
                [{'pmid': str(i)} for i in range(1, 21)],
                [{'pmid': str(i)} for i in range(20, 40)],
            ]):
            articles, start, end = main.select_articles()
        self.assertEqual(len(articles), 10)
        self.assertEqual(len({a['pmid'] for a in articles}), 10)
        self.assertTrue(any('Нейрореанимация' in a['areas'] for a in articles))
        self.assertEqual((end - start).days, 6)

    def test_unicode_messages_preserve_text_and_fit_telegram(self):
        original = '🧠' * 5000
        parts = list(main.split_messages(original, 1900))
        self.assertEqual(''.join(parts), original)
        self.assertTrue(all(len(p.encode('utf-16-le')) // 2 <= 4096 for p in parts))

    @patch.dict(os.environ, {'GEMINI_API_KEY': 'test-key', 'GEMINI_FREE_TIER': 'true'})
    def test_ai_quota_exhaustion(self):
        response = Mock()
        response.raise_for_status.side_effect = requests.HTTPError('secret URL')
        with patch.object(main.SESSION, 'post', return_value=response):
            self.assertIsNone(main.ai_summary({'title': 'A', 'abstract': 'B'}))

    @patch.dict(os.environ, {'GEMINI_API_KEY': 'test-key', 'GEMINI_FREE_TIER': 'false'})
    def test_ai_can_be_explicitly_disabled(self):
        with patch.object(main.SESSION, 'post') as post:
            self.assertIsNone(main.ai_summary({}))
            post.assert_not_called()

    @patch.dict(os.environ, {'TELEGRAM_BOT_TOKEN': 'test-secret', 'TELEGRAM_CHAT_ID': '123'})
    def test_delivery_requires_receipt(self):
        response = Mock(ok=True, status_code=200)
        response.json.return_value = {'ok': True, 'result': {}}
        with patch.object(main.SESSION, 'post', return_value=response):
            with self.assertRaisesRegex(RuntimeError, 'invalid delivery receipt'):
                main.send_telegram('test')

    @patch.dict(os.environ, {'TELEGRAM_BOT_TOKEN': 'test-secret', 'TELEGRAM_CHAT_ID': '123'})
    def test_transport_error_does_not_expose_token(self):
        with patch.object(main.SESSION, 'post', side_effect=requests.ConnectionError('test-secret')):
            with self.assertRaises(RuntimeError) as error:
                main.send_telegram('test')
        self.assertNotIn('test-secret', str(error.exception))
        self.assertIsNone(error.exception.__cause__)

    def test_fallback_survives_translation_failure(self):
        with patch.object(main.SESSION, 'get', side_effect=requests.Timeout):
            result = main.fallback_summary({'abstract': 'Results were inconclusive.'})
        self.assertIn('перевод недоступен', result)
        self.assertIn('Results were inconclusive.', result)

    def test_fallback_uses_structured_results_and_limitations(self):
        article = {'abstract': 'Background. Results. Conclusion.', 'sections': {
            'METHODS': 'Randomized trial with 120 participants.',
            'RESULTS': 'Mortality was 10% versus 20%.',
            'LIMITATIONS': 'Small sample size.',
            'CONCLUSIONS': 'Further trials are needed.',
        }}
        with patch.object(main, 'translate_text', side_effect=lambda text: (text, True)):
            summary = main.fallback_summary(article)
        self.assertIn('120 participants', summary)
        self.assertIn('10% versus 20%', summary)
        self.assertIn('Small sample size', summary)
        self.assertIn('Further trials are needed', summary)

    @patch.dict(os.environ, {'GEMINI_API_KEY': 'test-key', 'GEMINI_FREE_TIER': 'true'})
    def test_ai_valid_structured_response(self):
        import json
        response = Mock()
        fields = {'summary': 'Описание', 'results': 'Результаты',
                  'limitations': 'Ограничения', 'clinical_significance': 'Значимость'}
        response.json.return_value = {'candidates': [{'content': {'parts': [
            {'text': 'internal thought', 'thought': True},
            {'text': json.dumps(fields)},
        ]}}]}
        with patch.object(main.SESSION, 'post', return_value=response):
            summary = main.ai_summary({'title': 'A', 'abstract': 'B'})
        self.assertIn('Клиническая значимость: Значимость', summary)
        self.assertNotIn('internal thought', summary)

    def test_empty_pubmed_is_failure(self):
        with self.assertRaises(RuntimeError):
            main.make_digest([], 'start', 'end')


if __name__ == '__main__':
    unittest.main()
