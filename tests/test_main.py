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
                [{'pmid': str(i)} for i in range(39, 59)],
            ]):
            articles, start, end = main.select_articles()
        self.assertEqual(len(articles), 10)
        self.assertEqual(len({a['pmid'] for a in articles}), 10)
        for area in main.QUERIES:
            self.assertGreaterEqual(sum(area in a['areas'] for a in articles), 3)
        self.assertEqual((end - start).days, 6)

    def test_article_is_one_short_message_even_with_long_model_output(self):
        article = {'title': '🧠' * 600, 'journal': 'Journal' * 100, 'year': '2026',
                   'url': 'https://pubmed.ncbi.nlm.nih.gov/123/', 'abstract': 'Abstract',
                   'areas': list(main.QUERIES)}
        fields = {key: 'Очень длинное описание с числами 25% и ограничениями. ' * 100
                  for key in ('summary', 'results', 'limitations', 'clinical_significance')}
        with patch.object(main, 'ai_summary', return_value=main.format_summary(fields)):
            messages = main.make_digest([article], 'start', 'end')
        self.assertEqual(len(messages), 2)  # Header + exactly one paper.
        self.assertLessEqual(len(messages[1]), 1500)
        self.assertLessEqual(len(messages[1].encode('utf-16-le')) // 2, 4096)
        self.assertTrue(messages[1].endswith(article['url']))
        for label in ('Краткое содержание:', 'Результаты:', 'Ограничения:', 'Клиническая значимость:'):
            self.assertIn(label, messages[1])

    @patch.dict(os.environ, {'TELEGRAM_BOT_TOKEN': 'test-secret', 'TELEGRAM_CHAT_ID': '123'})
    def test_each_paper_has_exactly_one_send_request(self):
        response = Mock(ok=True, status_code=200)
        response.json.return_value = {'ok': True, 'result': {'message_id': 1, 'chat': {'id': 123}}}
        messages = ['Header', 'Paper 1', 'Paper 2']
        with patch.object(main.SESSION, 'post', return_value=response) as post, patch.object(main.time, 'sleep'):
            self.assertEqual(main.send_telegram(messages), 3)
        self.assertEqual([call.kwargs['json']['text'] for call in post.call_args_list], messages)

    def test_ranking_favors_clinical_evidence(self):
        trial = {'publication_types': ['Randomized Controlled Trial'], 'title': 'Treatment comparison'}
        case = {'publication_types': ['Case Reports'], 'title': 'A case report'}
        animal = {'publication_types': ['Journal Article'], 'mesh_terms': ['Animals'], 'title': 'Study in rats'}
        self.assertGreater(main.evidence_score(trial), main.evidence_score(case))
        self.assertGreater(main.evidence_score(trial), main.evidence_score(animal))
        self.assertGreater(main.evidence_score({'title': 'A systematic review'}), main.evidence_score(case))
        self.assertFalse(main.eligible_article({'publication_types': ['Retracted Publication']}))

    def test_review_mentioning_trials_is_not_ranked_as_trial(self):
        review = {'title': 'A narrative review', 'abstract': 'Randomized trials are needed.',
                  'publication_types': ['Review']}
        self.assertLess(main.evidence_score(review), 80)
        self.assertLess(main.evidence_score({'title': 'Is a Randomized Trial Feasible?'}), 80)
        self.assertFalse(main.eligible_article({'title': 'Protocol for a randomized trial'}))
        self.assertLess(main.evidence_score({'title': 'A comparative study',
                         'sections': {'METHODS': 'A non-randomized prospective study.'}}), 80)

    def test_off_topic_cardiovascular_and_urology_papers_are_excluded(self):
        vascular, spine, trauma = list(main.QUERIES)
        self.assertFalse(main.matches_topic({'title': 'Outcomes of a multibranch aortic device'}, spine))
        self.assertFalse(main.matches_topic({'title': 'Intradetrusor injections in spinal cord injury'}, trauma))
        self.assertFalse(main.matches_topic({'title': 'A narrative review of general intensive care'}, vascular))
        self.assertTrue(main.matches_topic({'title': 'Endovascular treatment of intracranial aneurysms'}, vascular))
        self.assertTrue(main.matches_topic({'title': 'Lumbar discectomy clinical trial'}, spine))
        self.assertTrue(main.matches_topic({'title': 'Outcomes of severe traumatic brain injury'}, trauma))

    def test_selection_prefers_high_ranked_paper_within_each_topic(self):
        pools = [[{'pmid': f'{topic}-{i}', 'publication_types': ['Case Reports']} for i in range(12)]
                 + [{'pmid': f'{topic}-trial', 'publication_types': ['Randomized Controlled Trial']}]
                 for topic in range(3)]
        with patch.object(main, 'pubmed_ids', return_value=['1']), patch.object(
            main, 'fetch_articles', side_effect=pools
        ):
            articles, _, _ = main.select_articles()
        self.assertTrue(all(a['pmid'].endswith('-trial') for a in articles[:3]))

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

    @patch.dict(os.environ, {'OPENROUTER_API_KEY': 'private-test-key'})
    def test_openrouter_uses_only_free_router(self):
        import json
        response = Mock()
        response.json.return_value = {'choices': [{'message': {'content': json.dumps({
            'summary': 'Описание', 'results': 'Результаты', 'limitations': 'Ограничения',
            'clinical_significance': 'Значимость',
        })}}]}
        with patch.object(main.SESSION, 'post', return_value=response) as post:
            result = main.openrouter_summary({'title': 'A', 'abstract': 'B'})
        self.assertIn('Краткое содержание', result)
        payload = post.call_args.kwargs['json']
        self.assertEqual(payload['model'], 'openrouter/free')
        self.assertNotIn('models', payload)
        self.assertNotIn('private-test-key', str(payload))

    def test_openrouter_failure_tries_gemini(self):
        with patch.object(main, 'openrouter_summary', return_value=None), patch.object(
            main, 'gemini_summary', return_value='Gemini result'
        ) as gemini:
            self.assertEqual(main.ai_summary({}), 'Gemini result')
            gemini.assert_called_once()

    def test_invalid_ai_payload_is_rejected(self):
        self.assertIsNone(main.format_summary([]))
        self.assertIsNone(main.format_summary({'summary': 'partial'}))

    @patch.dict(os.environ, {'OPENROUTER_API_KEY': 'private-test-key'})
    def test_openrouter_retries_incomplete_fields(self):
        import json
        bad = Mock(status_code=200)
        bad.json.return_value = {'choices': [{'message': {'content': '{}'}}]}
        good = Mock(status_code=200)
        good.json.return_value = {'choices': [{'message': {'content': json.dumps({
            'summary': 'Описание', 'results': 'Результаты', 'limitations': 'Ограничения',
            'clinical_significance': 'Значимость',
        })}}]}
        with patch.object(main.SESSION, 'post', side_effect=[bad, good]) as post:
            result = main.openrouter_summary({'title': 'A', 'abstract': 'B'})
        self.assertIsNotNone(result)
        self.assertEqual(post.call_count, 2)
        self.assertEqual(post.call_args.kwargs['json']['response_format']['type'], 'json_object')

    def test_one_invalid_paper_does_not_disable_remaining_ai(self):
        articles = [{'title': 'A', 'abstract': 'B', 'journal': 'J', 'year': '2026', 'url': 'url'}] * 2
        with patch.object(main, 'ai_summary', side_effect=[None, 'AI summary']) as ai, patch.object(
            main, 'fallback_summary', return_value='fallback'
        ):
            digest = main.make_digest(articles, 'start', 'end')
        self.assertEqual(ai.call_count, 2)
        self.assertTrue(any('AI summary' in message for message in digest))

    def test_empty_pubmed_is_failure(self):
        with self.assertRaises(RuntimeError):
            main.make_digest([], 'start', 'end')


if __name__ == '__main__':
    unittest.main()
