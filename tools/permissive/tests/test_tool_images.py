import copy
import unittest

from permissive.tool_images import vision_input


class ToolImageTests(unittest.TestCase):
    def test_call_results_keep_text_and_images_reach_backend_vision_role(self):
        image = {'type': 'input_image', 'image_url': 'data:image/png;base64,cGljdHVyZQ==', 'detail': 'high'}
        body = {'input': [{'type': 'function_call', 'call_id': 'pdf-page-call', 'name': 'render', 'arguments': '{}'},
                          {'type': 'function_call_output', 'call_id': 'pdf-page-call', 'output': [{'type': 'input_text', 'text': 'Page 2 metadata'}, image]},
                          {'type': 'custom_tool_call_output', 'call_id': 'second-call', 'output': [image]}]}
        original = copy.deepcopy(body)
        converted = vision_input(body)
        self.assertEqual(body, original)
        self.assertEqual(len(converted['input']), 5)
        self.assertEqual(converted['input'][1]['call_id'], 'pdf-page-call')
        self.assertEqual(converted['input'][1]['output'][0]['text'], 'Page 2 metadata')
        for index in [2, 4]:
            self.assertEqual(converted['input'][index]['role'], 'user')
            self.assertEqual(converted['input'][index]['content'][1], image)
            self.assertIn('not a new user request', converted['input'][index]['content'][0]['text'])
        self.assertFalse(any(part.get('type') == 'input_image' for part in converted['input'][1]['output']))

    def test_text_calls_original_user_images_and_order_remain_intact(self):
        body = {'input': [{'role': 'user', 'content': [{'type': 'input_image', 'image_url': 'unchanged'}]},
                          {'type': 'function_call_output', 'call_id': 'read-call', 'output': 'ordinary text'},
                          {'type': 'function_call_output', 'call_id': 'empty-call', 'output': []}]}
        self.assertEqual(vision_input(body), body)
        self.assertEqual(vision_input({'input': 'plain request'}), {'input': 'plain request'})


if __name__ == '__main__':
    unittest.main()
