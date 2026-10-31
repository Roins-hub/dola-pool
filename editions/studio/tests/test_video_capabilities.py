import importlib
import importlib.util
import json
import unittest


class VideoCapabilitiesTests(unittest.TestCase):
    def capabilities(self):
        self.assertIsNotNone(importlib.util.find_spec('video_capabilities'),
                             'model duration validation must be shared by every submission entry point')
        return importlib.import_module('video_capabilities')

    def test_existing_model_aliases_resolve_to_the_same_capabilities(self):
        cap = self.capabilities()
        cases = {
            'seedance-2.0': 'seedance-2.0',
            'seedance_v2.0': 'seedance-2.0',
            'seedance_20': 'seedance-2.0',
            'SEEDANCE-V20': 'seedance-2.0',
            ' seedance_2.0 ': 'seedance-2.0',
            'seedance-2.5': 'seedance-2.5',
            'seedance_v2.5': 'seedance-2.5',
            'seedance_25': 'seedance-2.5',
            'SEEDANCE-V25': 'seedance-2.5',
            ' seedance_2.5 ': 'seedance-2.5',
        }
        for alias, canonical in cases.items():
            with self.subTest(alias=alias):
                self.assertEqual(cap.normalize_model(alias), canonical)

    def test_fast_accepts_fifteen_and_experimental_thirty_for_every_alias(self):
        cap = self.capabilities()
        for model in ('seedance-2.0', 'seedance_v2.0', 'seedance_20', 'SEEDANCE-V20'):
            for duration in (15, 30):
                self.assertEqual(cap.validate_video_parameters(model, duration), ('seedance-2.0', duration))
            for duration in (20,):
                with self.subTest(model=model, duration=duration):
                    with self.assertRaisesRegex(ValueError, r'Fast.*5.*10'):
                        cap.validate_video_parameters(model, duration)

    def test_seedance_one_explicit_aliases_accept_five_ten_and_experimental_fifteen_seconds(self):
        cap = self.capabilities()
        aliases = ('seedance-1.0', 'seedance_v1.0', 'seedance_10', 'SEEDANCE-V10', ' ic_mini ')
        for model in aliases:
            with self.subTest(model=model):
                self.assertEqual(cap.normalize_model(model), 'seedance-1.0')
                self.assertEqual(cap.supported_durations(model), (5, 10, 15))
                for duration in (5, 10, 15):
                    self.assertEqual(cap.validate_video_parameters(model, duration), ('seedance-1.0', duration))
                for duration in (30,):
                    with self.assertRaisesRegex(ValueError, r'Seedance 1\.0.*5.*10'):
                        cap.validate_video_parameters(model, duration)

    def test_wire_model_helper_keeps_one_fast_and_two_five_distinct(self):
        cap = self.capabilities()
        wire = getattr(cap, 'to_wire_model', None)
        self.assertTrue(callable(wire), 'One shared wire-model mapping must cover every submission transport')
        for model, expected in (('seedance-1.0', 'ic_mini'), ('ic_mini', 'ic_mini'),
                                ('seedance_v1.0', 'ic_mini'), ('seedance-2.0', 'seedance_v2.0'),
                                ('seedance_25', 'seedance_v2.5')):
            self.assertEqual(wire(model), expected)
        for model in ('mini', 'fast', 'seedance_v2.0_std'):
            with self.assertRaises(ValueError):
                wire(model)

    def test_supported_requests_return_canonical_model_and_integer_duration(self):
        cap = self.capabilities()
        for model, durations in (('seedance_v2.0', (5, 10, 15, 30)),
                                 ('seedance_v2.5', (5, 10, 15, 30))):
            for duration in durations:
                with self.subTest(model=model, duration=duration):
                    self.assertEqual(cap.validate_video_parameters(model, duration),
                                     (model.replace('_v', '-'), duration))
        self.assertEqual(cap.validate_video_parameters('seedance_v2.0', None),
                         ('seedance-2.0', 10))
        self.assertEqual(cap.validate_video_parameters('seedance-2.5', '30'),
                         ('seedance-2.5', 30))

    def test_unknown_model_is_not_silently_treated_as_fast_or_two_five(self):
        cap = self.capabilities()
        for model in ('seedance-3.0', 'fast', 'mini', 'seedance_v2.0_std', '', None, 25):
            with self.subTest(model=model):
                with self.assertRaisesRegex(ValueError, '不支持的模型'):
                    cap.validate_video_parameters(model, 10)

    def test_unsupported_or_malformed_duration_is_rejected_without_rounding(self):
        cap = self.capabilities()
        for duration in (0, -5, 4, 20, 31, True, 10.5, '10.5', ''):
            with self.subTest(duration=duration):
                with self.assertRaises(ValueError):
                    cap.validate_video_parameters('seedance-2.5', duration)

    def test_public_capabilities_are_json_compatible_and_model_specific(self):
        cap = self.capabilities()
        wire = json.loads(json.dumps(cap.SUPPORTED_DURATIONS_BY_MODEL))
        self.assertEqual(wire, {'seedance-1.0': [5, 10, 15], 'seedance-2.0': [5, 10, 15, 30], 'seedance-2.5': [5, 10, 15, 30]})
        self.assertEqual(cap.supported_durations('seedance_v20'), (5, 10, 15, 30))
        self.assertEqual(cap.supported_durations('seedance_25'), (5, 10, 15, 30))


if __name__ == '__main__':
    unittest.main()
