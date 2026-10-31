import unittest

import video_capabilities as caps


class PluginExpert30CapabilitiesTests(unittest.TestCase):
    def test_expert_profile_accepts_four_distinct_models_and_preserves_wire_identity(self):
        validate = getattr(caps, 'validate_plugin_expert30_parameters', lambda *a, **k: {})
        for model, wire in [('seedance-2.5', 'seedance_v2.5'),
                            ('seedance-2.0', 'seedance_v2.0'),
                            ('seedance-2.0-std', 'seedance_v2.0_std'),
                            ('seedance-2.0-mini', 'seedance_v2.0_mini')]:
            with self.subTest(model=model):
                result = validate(model, 30, '21:9', '480p')
                self.assertEqual(result, dict(model=model, duration=30, ratio='21:9', resolution='480p', wire_model=wire))

    def test_expert_profile_is_available_without_broadening_ordinary_parameters(self):
        self.assertEqual(caps.submission_duration('seedance-2.5', 30, 'plugin-expert30'), 30)
        self.assertEqual(caps.submission_duration('seedance-2.0-mini', 30, 'plugin-expert30'), 30)
        for model in ('seedance-2.0-mini', 'seedance-2.0-std'):
            with self.assertRaises(ValueError):
                caps.validate_video_parameters(model, 30)

    def test_expert_accepts_each_plugin_duration_without_changing_the_requested_duration(self):
        for duration in (5, 10, 15, 30):
            for value in (duration, str(duration)):
                with self.subTest(duration=value):
                    result = caps.validate_plugin_expert30_parameters('seedance-2.0-mini', value)
                    self.assertEqual(result['duration'], duration)
                    self.assertEqual(caps.submission_duration('seedance-2.0-mini', value, 'plugin-expert30'), duration)

    def test_expert_rejects_unlisted_specifications(self):
        validate = getattr(caps, 'validate_plugin_expert30_parameters', lambda *a, **k: {})
        for args in [('seedance-2.0', 20, '16:9', '720p'),
                     ('seedance-2.0', True, '16:9', '720p'),
                     ('seedance-2.0', '15.0', '16:9', '720p'),
                     ('seedance-1.0', 30, '16:9', '720p'),
                     ('seedance-2.0', 30, '2:1', '720p'),
                     ('seedance-2.0', 30, '16:9', '4k')]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                validate(*args)

    def test_result_shape_uses_requested_duration_and_optional_two_part_rule(self):
        shape = getattr(caps, 'plugin_expert_result_shape', None)
        self.assertTrue(callable(shape), 'Submission and publication must share the same duration contract')
        cases = [([5.1], 5, False, True), ([10], 10, False, True),
                 ([15.104], 15, False, True), ([15, 15], 15, False, False),
                 ([15.104, 15.104], 30, False, True), ([30.2], 30, False, True),
                 ([30.2], 30, True, False), ([15, 15], 30, True, True),
                 ([15], 30, False, False), ([], 5, False, False),
                 ([5], 5, True, False), ([30, 15], 30, False, False)]
        for durations, requested, two_part, expected in cases:
            with self.subTest(durations=durations, requested=requested, two_part=two_part):
                self.assertEqual(shape(durations, requested, two_part), expected)


if __name__ == '__main__':
    unittest.main()
