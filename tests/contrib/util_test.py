import decimal
import collections
import json
import types
import unittest

from schwaby.contrib import util
from schwaby.contrib.util import (
    MAX_SIGN_SCALE, decode_decimal, HeuristicJsonDecoder,
    UnusableDecimalScale)
from schwaby.utils import SchwabError
from ..utils import no_duplicates


class HeuristicJsonDecoderTest(unittest.TestCase):
    def test_raw_string_decodes(self):
        self.assertEqual(HeuristicJsonDecoder().decode_json_string(
            r'{"\\\\\\\\": "test"}'),
            {r'\\\\': 'test'})


    def test_replace_backslashes(self):
        # TODO: Actually collect some failing use cases...
        pass


class DecodeDecimalTest(unittest.TestCase):
    """The ACCT_ACTIVITY scaled-integer decimal.

    Two kinds of case, and the difference matters when reading a failure. The
    shapes are taken from live traffic, the values in them are invented, and
    each pins one of the five things this gets right, each of which costs a
    value, usually as a wrong number rather than an error. The *corruption* is
    synthetic, necessarily: a guard
    against a shape nothing currently sends cannot be tested with what is
    currently sent. Anything reasoned rather than observed says so on the case.
    """

    @no_duplicates
    def test_payloads_in_the_measured_shapes_decode_exactly(self):
        for field, expected, label in (
                ({'lo': '7350000', 'signScale': 12}, '7.35', 'LimitPrice'),
                ({'lo': '63451700', 'signScale': 12}, '63.4517', 'AveragePrice'),
                ({'lo': '118000000', 'signScale': 12}, '118', 'CumulativeQuantity'),
                ({'lo': '21470000', 'signScale': 12}, '21.47', 'Bid')):
            with self.subTest(label):
                self.assertEqual(decimal.Decimal(expected),
                                 decode_decimal(field))

    @no_duplicates
    def test_lo_carries_the_whole_mantissa_past_32_bits(self):
        # Measured shape, invented values. In recorded ACCT_ACTIVITY traffic,
        # 5,843 decimal objects carried `lo` alone in every one, as a
        # string of digits, and 129 were wider than 32 bits: fill
        # `PrincipalAmmount`, and the estimated amounts on OrderCreated, ten
        # digits at signScale 12 or 13. Read whole, each of the 33 fill
        # principals equalled price times quantity from the same message.
        # Holding `lo` to 32 bits would refuse all 129.
        for field, expected in (
                ({'lo': '4294967296', 'signScale': 12}, '4294.967296'),
                ({'lo': '7350000000', 'signScale': 12}, '7350'),
                ({'lo': '7350000000', 'signScale': 13}, '-7350'),
                ({'lo': '9999999999', 'signScale': 12}, '9999.999999')):
            with self.subTest(value=field):
                self.assertEqual(decimal.Decimal(expected),
                                 decode_decimal(field))
        # Positive control on the widest value that decoded before.
        self.assertEqual(decimal.Decimal('4294.967295'),
                         decode_decimal({'lo': '4294967295', 'signScale': 12}))

    @no_duplicates
    def test_a_lone_lo_is_bounded_by_the_dotnet_mantissa(self):
        # 96 bits is the widest mantissa a System.Decimal has. The longest
        # `lo` measured was ten digits, so this bound is the encoding's rather
        # than the sample's.
        widest = 2 ** 96 - 1
        for member in (widest, str(widest), '000' + str(widest)):
            with self.subTest(value=member):
                self.assertEqual(decimal.Decimal(widest),
                                 decode_decimal({'lo': member}))
        for member in (2 ** 96, str(2 ** 96), '1' + '0' * 29):
            with self.subTest(value=member):
                with self.assertRaises(UnusableDecimalScale):
                    decode_decimal({'lo': member, 'signScale': 12})
        # Past `int()`'s digit limit, which raises a bare ValueError rather
        # than a SchwabError unless the length is checked first.
        with self.assertRaises(UnusableDecimalScale):
            decode_decimal({'lo': '9' * 5000, 'signScale': 12})

    @no_duplicates
    def test_beside_mid_or_hi_each_member_is_still_32_bits(self):
        # A `lo` wider than 32 bits next to a `mid` or `hi` is two layouts at
        # once: read whole it is one number, read as a member it overlaps the
        # next one. No payload has carried `mid` or `hi` at all, so nothing
        # says which was meant, and it is refused rather than guessed. The
        # last case is the one a layout chosen on `mid` alone would get wrong,
        # by reading `lo` whole and dropping `hi`.
        for value in ({'lo': str(2 ** 32), 'mid': 1, 'signScale': 12},
                      {'lo': '1', 'mid': str(2 ** 32), 'signScale': 12},
                      {'lo': '1', 'hi': 2 ** 32, 'signScale': 12},
                      {'lo': str(2 ** 32), 'hi': 1}):
            with self.subTest(value=value):
                with self.assertRaises(UnusableDecimalScale):
                    decode_decimal(value)
        # A corrupt `mid` or `hi` is named as itself, not blamed on a `lo`
        # that is ordinary on its own.
        for value, name in (({'lo': '7350000000', 'hi': False}, 'hi'),
                            ({'lo': '7350000000', 'mid': ''}, 'mid')):
            with self.subTest(named=name):
                with self.assertRaisesRegex(UnusableDecimalScale,
                                            '^' + name + ' is not'):
                    decode_decimal(value)
        # Positive control: the same slots at 32 bits decode.
        self.assertEqual(decimal.Decimal('8589.934591'),
                         decode_decimal({'lo': str(2 ** 32 - 1), 'mid': 1,
                                         'signScale': 12}))

    @no_duplicates
    def test_an_int_subclass_decodes_as_its_value(self):
        # `json.loads` never produces one, but a custom decoder can. The
        # member is formatted into the result, and a subclass decides its own
        # `str` and `format`, so unless it is reduced to a plain int it can
        # decode as another number, or raise past the one `except` callers
        # are told to write. The three-member path reduced it by accident,
        # through `<<`; the lone-lo path did not.
        class Shown(int):
            def __str__(self):
                return '999'
            __repr__ = __str__

            def __format__(self, spec):
                return '999'

        class Raises(int):
            def __str__(self):
                raise RuntimeError('boom')
            __repr__ = __str__

            def __format__(self, spec):
                raise RuntimeError('boom')

        for cls in (Shown, Raises):
            with self.subTest(member=cls.__name__):
                self.assertEqual(
                        decimal.Decimal('0.000005'),
                        decode_decimal({'lo': cls(5), 'signScale': 12}))
                self.assertEqual(
                        decimal.Decimal('4294.967301'),
                        decode_decimal({'lo': cls(5), 'mid': cls(1),
                                        'signScale': 12}))
                self.assertEqual(
                        decimal.Decimal('-0.000005'),
                        decode_decimal({'lo': cls(5), 'signScale': cls(13)}))
                # Out of range, and the refusal cannot raise on it either.
                with self.assertRaises(UnusableDecimalScale):
                    decode_decimal({'lo': '1', 'signScale': cls(65)})

    @no_duplicates
    def test_a_mapping_that_keeps_its_data_elsewhere_decodes(self):
        # `DotMap` subclasses OrderedDict and keeps its data in its own store,
        # so dict's storage is empty. Read through `dict.items`, a $5,000
        # principal decoded as a clean zero -- the worst wrong answer on this
        # feed. Read through the mapping's own `items()`, it is the value.
        class Stored(dict):
            def __init__(self, **fields):
                super().__init__()
                self._store = dict(fields)

            def items(self):
                return self._store.items()

            def get(self, key, default=None):
                return self._store.get(key, default)

            def __iter__(self):
                return iter(self._store)

            def __len__(self):
                return len(self._store)

        self.assertEqual([], list(dict.items(Stored(lo='1'))))   # the trap
        self.assertEqual(decimal.Decimal('-5000'),
                         decode_decimal(Stored(lo='5000000000',
                                               signScale=13)))
        # Any mapping, not only a dict.
        for mapping in (collections.UserDict(lo='7350000', signScale=12),
                        types.MappingProxyType(
                            {'lo': '7350000', 'signScale': 12})):
            with self.subTest(mapping=type(mapping).__name__):
                self.assertEqual(decimal.Decimal('7.35'),
                                 decode_decimal(mapping))

    @no_duplicates
    def test_a_str_subclass_key_is_its_plain_text(self):
        self.addCleanup(util._reported_keys.clear)

        class Key(str):
            # str's own hash, so two of these never collide while the test
            # builds its dict -- only the decoder could reach `__eq__`.
            __hash__ = str.__hash__

            def __eq__(self, other):
                raise RuntimeError('boom')

        self.assertEqual(decimal.Decimal('7.35'),
                         decode_decimal({Key('lo'): '7350000',
                                         Key('signScale'): 12}))

        # Two keys with the same plain text cannot both be the member. This
        # pair can share a dict because it hashes and compares by identity.
        class Twin(str):
            __eq__ = object.__eq__

            def __hash__(self):
                return 7

        with self.assertRaises(UnusableDecimalScale):
            decode_decimal({Twin('lo'): '1', 'lo': '2'})

    @no_duplicates
    def test_a_claimed_class_is_not_believed(self):
        # `isinstance` also believes `__class__`, so each of these passed a
        # type check and then failed the real type's own method with a bare
        # TypeError, past the one `except` callers write.
        def claiming(cls):
            class Claim:
                @property
                def __class__(self):
                    return cls
            return Claim()

        from unittest.mock import MagicMock
        for value in (MagicMock(spec=dict), claiming(str), claiming(int),
                      claiming(float), claiming(decimal.Decimal),
                      {'lo': claiming(str), 'signScale': 12},
                      {'lo': MagicMock(spec=int), 'signScale': 12}):
            with self.subTest(value=type(value).__name__):
                with self.assertRaises(UnusableDecimalScale):
                    decode_decimal(value)

    @no_duplicates
    def test_an_unnameable_type_does_not_escape_the_message(self):
        # The fallback that names an unprintable value read `type(x).__name__`,
        # which consults the metaclass first.
        self.addCleanup(util._reported_keys.clear)

        class Meta(type):
            @property
            def __name__(cls):
                raise RuntimeError('boom')

        class Unprintable(metaclass=Meta):
            def __str__(self):
                raise RuntimeError('boom')
            __repr__ = __str__

        with self.assertRaises(UnusableDecimalScale):
            decode_decimal({'lo': '1', Unprintable(): 0})
        with self.assertRaises(UnusableDecimalScale):
            decode_decimal(Unprintable())

        # A class's `__name__` can be a str subclass, which the descriptor
        # returns as it is; its own `__len__` ran in the same fallback.
        class Name(str):
            def __len__(self):
                raise RuntimeError('boom')

        class Renamed:
            def __repr__(self):
                raise RuntimeError('boom')
            __str__ = __repr__

        Renamed.__name__ = Name('Renamed')
        with self.assertRaises(UnusableDecimalScale):
            decode_decimal(Renamed())
        with self.assertRaises(UnusableDecimalScale):
            decode_decimal({'lo': '1', Renamed(): 0})

    @no_duplicates
    def test_quoted_key_names_stay_bounded(self):
        # Quoting escapes a character to as many as ten, so the size bound on
        # the refusal has to hold for the alphabet that expands, not only the
        # one that does not.
        self.addCleanup(util._reported_keys.clear)
        keys = {'\U000e0001' * 70 + str(i): 0 for i in range(9)}
        for value in (dict(keys, lo='1'), keys):
            with self.subTest(with_lo='lo' in value):
                with self.assertRaises(UnusableDecimalScale) as caught:
                    decode_decimal(value)
                self.assertLess(len(str(caught.exception)), 1500)
                self.assertNotIn('\n', str(caught.exception))

    @no_duplicates
    def test_an_unhashable_class_is_refused_not_escaped(self):
        # Routing asks whether the type is a Mapping, and the ABC's cache
        # hashes the class. A metaclass defining `__eq__` alone makes every
        # class it creates unhashable, which escaped as a bare TypeError.
        class EqMeta(type):
            def __eq__(cls, other):
                return cls is other

        class Plain(metaclass=EqMeta):
            pass

        with self.assertRaises(UnusableDecimalScale):
            decode_decimal(Plain())

    @no_duplicates
    def test_a_values_own_repr_cannot_forge_a_log_line(self):
        # Every refusal ends with the value's repr. A plain dict escapes a
        # newline; a value's own `__repr__` need not, and a mapping type or a
        # str subclass from a custom decoder supplies its own.
        self.addCleanup(util._reported_keys.clear)
        forged = 'x\nCRITICAL:schwaby.orders:order 123 FILLED'

        class Loud(str):
            def __repr__(self):
                return str.__str__(self)

        class Shouting(collections.UserDict):
            def __repr__(self):
                return forged

        for value in ({'lo': Loud(forged), 'signScale': 12},
                      Shouting(lo='1', extra=0)):
            with self.subTest(value=type(value).__name__):
                with self.assertRaises(UnusableDecimalScale) as caught:
                    decode_decimal(value)
                self.assertNotIn('\n', str(caught.exception))
                # Positive control: the value is still described, escaped.
                self.assertIn('CRITICAL:schwaby.orders', str(caught.exception))

        # A class name reaches the same text when the repr itself raises.
        def unformattable(self):
            raise RuntimeError('boom')

        Named = type('X>' + forged + '<', (), {'__repr__': unformattable})
        with self.assertRaises(UnusableDecimalScale) as caught:
            decode_decimal(Named())
        self.assertNotIn('\n', str(caught.exception))
        self.assertIn('CRITICAL:schwaby.orders', str(caught.exception))

        # And a class name is bounded before it is used.
        Long = type('N' * 100000, (), {'__repr__': unformattable})
        with self.assertRaises(UnusableDecimalScale) as caught:
            decode_decimal(Long())
        self.assertLess(len(str(caught.exception)), 400)

        # And bounded after escaping, which can take one character to ten.
        class Wide:
            def __repr__(self):
                return '\U000e0001' * 500

        with self.assertRaises(UnusableDecimalScale) as caught:
            decode_decimal(Wide())
        self.assertLess(len(str(caught.exception)), 400)

    @no_duplicates
    def test_the_once_per_key_report_line_is_escaped(self):
        # The report line quotes each name; a newline in a key must arrive
        # escaped, not as a second line.
        util._reported_keys.clear()
        self.addCleanup(util._reported_keys.clear)
        forged = 'x\r\nCRITICAL:schwaby.orders:order 123 FILLED'
        with self.assertLogs(util.get_logger(), level='WARNING') as got:
            with self.assertRaises(UnusableDecimalScale):
                decode_decimal({'lo': '1', forged: 0})
        report = [line for line in got.output
                  if 'inside a decimal object' in line]
        self.assertEqual(1, len(report))
        self.assertNotIn('\n', report[0])
        self.assertNotIn('\r', report[0])
        self.assertIn('CRITICAL:schwaby.orders', report[0])

    @no_duplicates
    def test_the_once_per_key_report_line_is_bounded(self):
        # The exception's key names were capped and the log line's were not,
        # so thirty-two non-printable keys logged twenty kilobytes at once.
        util._reported_keys.clear()
        self.addCleanup(util._reported_keys.clear)
        value = {'%02d' % i + '\U000e0001' * 62: 0 for i in range(32)}
        value['lo'] = '1'
        with self.assertLogs(util.get_logger(), level='WARNING') as got:
            with self.assertRaises(UnusableDecimalScale):
                decode_decimal(value)
        self.assertLess(max(len(line) for line in got.output), 4000)

    @no_duplicates
    def test_a_huge_bare_exponent_is_refused_not_escaped(self):
        # `decimal.Decimal` raises InvalidOperation -- neither a SchwabError
        # nor a ValueError -- on an exponent of 10**18 or more, before the
        # magnitude check can run. Reachable from any JSON string field.
        for bad in ('1E1000000000000000000', '-1E1000000000000000000',
                    '1.0E1000000000000000000', '0E-99999999999999999999',
                    '1E' + '9' * 5000):
            with self.subTest(value=bad[:30]):
                with self.assertRaises(UnusableDecimalScale):
                    decode_decimal(bad)
        # Positive control: a padded exponent inside the bound still decodes.
        self.assertEqual(decimal.Decimal('100'),
                         decode_decimal('1E' + '0' * 30 + '2'))
        self.assertEqual(decimal.Decimal('0.01'), decode_decimal('1E-2'))

    @no_duplicates
    def test_a_refusal_cannot_forge_a_log_line(self):
        # Key names are venue text. Joined bare into the message, a newline in
        # one forged a second line wherever the exception was logged --
        # including through the recipe the streaming docs give for it.
        self.addCleanup(util._reported_keys.clear)
        forged = 'x\nCRITICAL:schwaby.orders:order 123 FILLED'
        for value in ({'lo': '7350000', 'signScale': 12, forged: 0},
                      {forged: 0}):
            with self.subTest(value=value):
                with self.assertRaises(UnusableDecimalScale) as caught:
                    decode_decimal(value)
                self.assertNotIn('\n', str(caught.exception))
                # Positive control: the key is still named, quoted.
                self.assertIn(repr(forged), str(caught.exception))

    @no_duplicates
    def test_subclasses_decode_as_their_plain_values(self):
        # `json.loads` never produces a subclass; a custom decoder can. Each
        # of these decided its own value, or raised past the one `except`
        # callers write, until inputs were reduced to exact built-in types.
        class Text(str):
            def isdigit(self):
                return True

            def lstrip(self, *args):
                return '999'

            def __str__(self):
                raise RuntimeError('boom')

        class Real(float):
            def __str__(self):
                return '999'
            __repr__ = __str__

        class Whole(int):
            def bit_length(self):
                raise RuntimeError('boom')

        class Exact(decimal.Decimal):
            def is_finite(self):
                raise RuntimeError('boom')

        self.assertEqual(decimal.Decimal('0.000005'),
                         decode_decimal({'lo': Text('5'), 'signScale': 12}))
        with self.assertRaises(UnusableDecimalScale):
            decode_decimal({'lo': Text('1_0'), 'signScale': 12})
        self.assertEqual(decimal.Decimal('5'), decode_decimal(Real(5.0)))
        self.assertEqual(decimal.Decimal(5), decode_decimal(Whole(5)))
        self.assertEqual(decimal.Decimal('5'), decode_decimal(Exact('5')))
        self.assertIs(decimal.Decimal, type(decode_decimal(Exact('5'))))

        # And the text a refusal is built from. `str()` and `repr()` hand back
        # a subclass untouched when the method returns one, so its own
        # `__hash__` and `__len__` ran inside the code that names the problem.
        class Name(str):
            def __hash__(self):
                raise RuntimeError('boom')

            def __len__(self):
                raise RuntimeError('boom')

        class Key:
            def __str__(self):
                return Name('odd')

        class Shown:
            def __repr__(self):
                return Name('odd')

        self.addCleanup(util._reported_keys.clear)
        with self.assertRaises(UnusableDecimalScale):
            decode_decimal({'lo': '1', Key(): 0})
        with self.assertRaises(UnusableDecimalScale):
            decode_decimal(Shown())

        # A bare str subclass too: its own `isascii` ran before the pattern.
        class Ascii(str):
            def isascii(self):
                raise RuntimeError('boom')

        self.assertEqual(decimal.Decimal('5'), decode_decimal(Ascii('5')))

    @no_duplicates
    def test_a_mapping_is_read_once_from_what_it_stores(self):
        class Hostile(dict):
            def __iter__(self):
                raise RuntimeError('boom')

            def items(self):
                raise RuntimeError('boom')

            def keys(self):
                raise RuntimeError('boom')

            def get(self, *args):
                raise RuntimeError('boom')

        class Fickle(dict):
            # `mid` on the first read, absent on every read after.
            reads = []

            def get(self, key, default=None):
                if key == 'mid':
                    Fickle.reads.append(key)
                    return 1 if len(Fickle.reads) == 1 else None
                return dict.get(self, key, default)

        class Unequal:
            def __hash__(self):
                return hash('lo')

            def __eq__(self, other):
                raise RuntimeError('boom')

        self.addCleanup(util._reported_keys.clear)
        # A mapping whose `items()` cannot be read is refused, not bypassed:
        # bypassing it through dict's storage is what decoded a mapping that
        # keeps its data elsewhere as zero.
        with self.assertRaises(UnusableDecimalScale):
            decode_decimal(Hostile(lo='5', signScale=12))
        self.assertEqual(decimal.Decimal('4294.967301'),
                         decode_decimal(Fickle(lo='5', mid=1, signScale=12)))
        with self.assertRaises(UnusableDecimalScale):
            decode_decimal({Unequal(): '5', 'signScale': 12})
        # The ordinary subclass a custom decoder produces still decodes.
        self.assertEqual(
                decimal.Decimal('7.35'),
                decode_decimal(json.loads(
                    '{"lo": "7350000", "signScale": 12}',
                    object_pairs_hook=collections.OrderedDict)))

    @no_duplicates
    def test_an_odd_signscale_is_negative(self):
        # Schwab's own worked example, and the field it was observed on:
        # principal is negative on a buy because the cash goes out.
        self.assertEqual(decimal.Decimal('-40'),
                         decode_decimal({'lo': '40000000', 'signScale': 13}))
        self.assertEqual(decimal.Decimal('-7.35'),
                         decode_decimal({'lo': '7350000', 'signScale': 13}))

    @no_duplicates
    def test_a_mantissa_without_a_scale_is_scale_zero(self):
        # This was a raise, on the reasoning that a scale which is missing
        # cannot be told from one that was lost, and that guessing the common
        # value -- six places -- is wrong by a factor of a million when it is
        # wrong. The reasoning was fine and the premise was not: the shape is
        # ordinary traffic, on the size fields of an ordinary quote.
        #
        # One quote, in the shape live traffic carries it, with invented
        # values. Scale 12 on the prices and no scale at all on the sizes, in
        # the same object:
        quote = {'Ask':     {'lo': '21480000', 'signScale': 12},
                 'AskSize': {'lo': '15300'},
                 'Bid':     {'lo': '21470000', 'signScale': 12},
                 'BidSize': {'lo': '2600'},
                 'Mid':     {'lo': '21475000', 'signScale': 12}}
        self.assertEqual(
                {'Ask': decimal.Decimal('21.48'),
                 'AskSize': decimal.Decimal(15300),
                 'Bid': decimal.Decimal('21.47'),
                 'BidSize': decimal.Decimal(2600),
                 'Mid': decimal.Decimal('21.475')},
                {k: decode_decimal(v) for k, v in quote.items()})
        # $21.48 with 15300 on the ask is a coherent quote; 0.0153 shares is
        # not. The serializer omits what is zero, and the scale is no
        # different from a mantissa member in that respect.

    @no_duplicates
    def test_a_mantissa_less_object_is_zero_not_unknown(self):
        # Measured on three fields of one payload, the third of which is the
        # hazard: LeavesQuantity on the *final* fill event of a completed
        # order. Read as unknown, a complete fill reports as outstanding.
        self.assertEqual(decimal.Decimal(0), decode_decimal({'signScale': 12}))
        self.assertEqual(decimal.Decimal(0), decode_decimal({}))

    @no_duplicates
    def test_the_mantissa_spans_three_fields(self):
        # Reasoned from the .NET layout, not observed. No captured payload has
        # carried `mid` or `hi` at all -- Schwab sends the whole mantissa in
        # `lo` -- so this is the reading for a payload that ever does, and
        # the only one those names support.
        self.assertEqual(
                decimal.Decimal('5000'),
                decode_decimal({'lo': '705032704', 'mid': 1, 'signScale': 12}))
        self.assertEqual(
                decimal.Decimal('18446744073709.551616'),
                decode_decimal({'lo': '0', 'mid': 0, 'hi': 1,
                                'signScale': 12}))

    @no_duplicates
    def test_it_returns_decimal_so_the_value_can_be_repriced(self):
        # The point of not returning a float: set_price refuses one, so a
        # decoded value has to survive being fed straight back.
        from schwaby.orders.generic import OrderBuilder
        price = decode_decimal({'lo': '7350000', 'signScale': 12})
        self.assertIsInstance(price, decimal.Decimal)
        self.assertEqual({'price': '7.350000'},
                         OrderBuilder().set_price(price).build())

    @no_duplicates
    def test_a_hostile_scale_does_not_hang(self):
        # `10 ** (signScale // 2)` on this builds an astronomical integer and
        # hangs the thread, which a per-item try/except cannot rescue -- one
        # bad field would take a stream down rather than one message.
        #
        # `contrib/decimal-scale-is-bounded` red-proofs this. The bound is
        # explicit rather than left to `scaleb`'s own operand limit, which
        # could not be red-proofed: mutating it would hang the run rather
        # than fail the test.
        with self.assertRaises(UnusableDecimalScale):
            decode_decimal({'lo': '1', 'signScale': 10 ** 9})

    @no_duplicates
    def test_a_bare_number_passes_through(self):
        # The same field does not always arrive in the same shape.
        self.assertEqual(decimal.Decimal('7.35'), decode_decimal('7.35'))
        self.assertEqual(decimal.Decimal('7.35'), decode_decimal(7.35))
        self.assertEqual(decimal.Decimal('7350000'), decode_decimal(7350000))
        self.assertEqual(decimal.Decimal('7.35'),
                         decode_decimal(decimal.Decimal('7.35')))
        self.assertIsNone(decode_decimal(None))
        # A float goes through `str` so its binary expansion does not come
        # along: Decimal(0.1) is 0.1000000000000000055511151231257827.
        self.assertEqual(decimal.Decimal('0.1'), decode_decimal(0.1))

    @no_duplicates
    def test_the_callers_decimal_context_cannot_change_the_answer(self):
        # `scaleb` applies the *caller's* context. A consumer setting
        # `getcontext().prec = 6` somewhere else in their process -- an
        # ordinary thing to do when formatting money -- got 1234.57 for a
        # price of 1234.5678, silently, from a function whose whole purpose is
        # that its result feeds back into set_price.
        # Both branches. Building the value from a string and then negating
        # it would not do: `Decimal.__neg__` is itself a context-aware
        # operation, so it would be exact for positive values and round
        # negative ones. A fixture with only even signScales would never see
        # that, so this one has both.
        cases = ((12, '1234.5678'), (13, '-1234.5678'))
        original = decimal.getcontext().prec
        try:
            for scale, expected in cases:
                for prec in (1, 3, 6, 28, 60):
                    with self.subTest(signScale=scale, prec=prec):
                        decimal.getcontext().prec = prec
                        self.assertEqual(
                                decimal.Decimal(expected),
                                decode_decimal({'lo': '1234567800',
                                                'signScale': scale}))
        finally:
            decimal.getcontext().prec = original

    @no_duplicates
    def test_a_full_width_mantissa_does_not_round(self):
        # 96 bits is the widest a .NET Decimal carries, and it rounds under
        # the default precision of 28 if the value goes through the context.
        mantissa = 2 ** 96 - 1
        for scale, sign in ((12, ''), (13, '-')):
            with self.subTest(signScale=scale):
                field = {'lo': str(mantissa & 0xFFFFFFFF),
                         'mid': (mantissa >> 32) & 0xFFFFFFFF,
                         'hi': mantissa >> 64,
                         'signScale': scale}
                self.assertEqual(
                        decimal.Decimal('{}{}E-6'.format(sign, mantissa)),
                        decode_decimal(field))

    @no_duplicates
    def test_an_implausible_scale_raises_rather_than_underflowing_to_zero(self):
        # Relying on the arithmetic to refuse is not enough: `scaleb`'s operand
        # limit only trips far out, so every scale from roughly two million to
        # four million underflowed to a zero that compares equal to zero. On
        # this feed that is the worst wrong answer available -- a corrupt
        # LeavesQuantity reading as zero is a complete fill.
        for scale in (MAX_SIGN_SCALE + 1, 2000100, 4000000, 10 ** 9, -1):
            with self.subTest(signScale=scale):
                with self.assertRaises(UnusableDecimalScale):
                    decode_decimal({'lo': '1', 'signScale': scale})

        # The bound itself is usable, so the assertions above are about
        # implausible scales rather than about the check refusing everything.
        self.assertEqual(decimal.Decimal(1),
                         decode_decimal({'lo': '1', 'signScale': 0}))
        self.assertIsNotNone(
                decode_decimal({'lo': '1', 'signScale': MAX_SIGN_SCALE}))

    @no_duplicates
    def test_a_mantissa_carried_only_in_mid_is_not_read_as_zero(self):
        # The serializer omits zero members, so under the three-member layout
        # a value whose low 32 bits are zero would arrive without `lo`.
        # Keying the mantissa-less guard on `lo` alone decoded that as zero.
        # Reasoned, not observed: no captured payload has carried `mid`.
        self.assertEqual(decimal.Decimal('4294.967296'),
                         decode_decimal({'mid': 1, 'signScale': 12}))
        self.assertEqual(decimal.Decimal('4294.967296'),
                         decode_decimal({'lo': '0', 'mid': 1, 'signScale': 12}))

    @no_duplicates
    def test_every_failure_is_catchable_by_one_except(self):
        # `decimal.InvalidOperation` is an ArithmeticError, so it is neither a
        # SchwabError nor a ValueError and escapes both handlers a caller
        # would reasonably write -- while the prose tells them to wrap each
        # field so one odd value does not cost the message. The empty string
        # reaches here from the SUBSCRIBED ack.
        # Enumerated from the ways a *member* can be wrong rather than from
        # the inputs the code already converts -- a list of the latter agrees
        # with the implementation by construction and cannot find a hole.
        for bad in ('', 'not a number',
                    {'lo': '1', 'signScale': 10 ** 9},
                    {'Lo': '1', 'SignScale': 12},        # not a decimal object
                    {'lo': 'x', 'signScale': 12},        # non-numeric string
                    {'lo': '1', 'signScale': 'x'},       # non-numeric scale
                    {'lo': [1], 'signScale': 12},        # non-scalar: TypeError
                    {'lo': '', 'signScale': 12},         # present but falsy
                    {'lo': False, 'signScale': 12},      # a bool is not one
                    {'lo': float('inf'), 'signScale': 12},  # not an integer
                    {'lo': '-1', 'signScale': 12},       # negative mantissa
                    {'lo': '1', 'signScale': True},      # a bool is not one
                    {'lo': '1', 'signScale': 1.9},       # nor is a float
                    {'lo': 1.9, 'signScale': 12},        # nor is a float
                    {'lo': '1_0', 'signScale': 12},      # PEP 515 underscore
                    {'lo': '\u00b2', 'signScale': 12},   # isdigit, int refuses
                    {'lo': '\u0663', 'signScale': 12},   # isdigit, int accepts
                    {'lo': str(2 ** 96), 'signScale': 12},  # past a mantissa
                    {'lo': str(2 ** 32), 'mid': 1},      # past a member
                    'NaN', 'Infinity', float('nan'), float('inf')):
            with self.subTest(value=bad):
                with self.assertRaises(SchwabError):
                    decode_decimal(bad)
                with self.assertRaises(ValueError):
                    decode_decimal(bad)

    def test_a_corrupt_member_is_refused_rather_than_coerced(self):
        # The failure mode this guards is not an escaping exception -- it is
        # the corruption that decodes. `int(1.9)` is 1, `int('1_0')` is 10 and
        # `int('\u0663')` is 3, so coerced, each of these would be a plausible
        # wrong number on a money field, silently.
        #
        # Every case carries its own positive control: the same object with a
        # sound member in the same slot must decode to a known value. Without
        # it an assertRaises passes just as well when the fixture never
        # reached the member at all.
        for name in ('lo', 'mid', 'hi'):
            # A lone `lo` is the whole mantissa and may be 96 bits wide; a
            # `mid` or `hi` is one 32-bit member of the three-member layout.
            too_wide = 2 ** 96 if name == 'lo' else 2 ** 32
            for corrupt in (1.9, float('inf'), True, False, '-1', '1_0',
                            ' 1', '1 ', '', '0x10', '\u00b2', '\u0663',
                            [1], {'a': 1}, too_wide, -1):
                with self.subTest(member=name, value=corrupt):
                    with self.assertRaises(UnusableDecimalScale):
                        decode_decimal({name: corrupt, 'signScale': 12})
            self.assertEqual(
                    {'lo': decimal.Decimal('0.000001'),
                     'mid': decimal.Decimal('4294.967296'),
                     'hi': decimal.Decimal('18446744073709.551616')}[name],
                    decode_decimal({name: '1', 'signScale': 12}))

    def test_a_corrupt_scale_is_refused_rather_than_coerced(self):
        for corrupt in (1.9, float('inf'), True, False, '-1', '1_0', ' 1',
                        '', '0x10', '\u00b2', '\u0663', [1], 2 ** 32, -1):
            with self.subTest(value=corrupt):
                with self.assertRaises(UnusableDecimalScale):
                    decode_decimal({'lo': '1', 'signScale': corrupt})
        # Positive control in the same slot.
        self.assertEqual(decimal.Decimal('0.000001'),
                         decode_decimal({'lo': '1', 'signScale': 12}))
        self.assertEqual(decimal.Decimal('-0.000001'),
                         decode_decimal({'lo': '1', 'signScale': 13}))

    def test_a_bare_value_refuses_what_a_member_refuses(self):
        # The bare-value path must refuse what the member path refuses:
        # `decimal.Decimal`'s parser accepts more than
        # `int`'s does, so every token the mantissa path refuses decoded here
        # instead, as a confident wrong number, on a field the docstring says
        # arrives in either shape.
        #
        # Driven from the same list against both paths, so the two cannot
        # drift apart again without a failure.
        for token in ('1_0',        # ten, under PEP 515
                      '12_',        # twelve -- `int` refuses this outright
                      '_12', '1__0', '1_0.5',
                      '\u0663',     # Arabic-Indic three
                      '\uff11\uff12',  # fullwidth twelve
                      '\u06f9',     # Extended Arabic-Indic nine
                      ' 12 ', '\t12\n', '+12',
                      '1e1_0', '1E\u0663',
                      '0x10', '1j', 'inf', 'nan'):
            with self.subTest(token=token):
                with self.assertRaises(UnusableDecimalScale):
                    decode_decimal(token)
                with self.assertRaises(UnusableDecimalScale):
                    decode_decimal({'lo': token, 'signScale': 12})
        # Positive control on both paths, from the same shape of input.
        self.assertEqual(decimal.Decimal('12'), decode_decimal('12'))
        self.assertEqual(decimal.Decimal('0.000012'),
                         decode_decimal({'lo': '12', 'signScale': 12}))

    def test_a_bare_value_that_is_not_a_number_at_all_is_refused(self):
        for value in (True, False, [1], {1, 2}, (1,), object(), b'12',
                      bytearray(b'12')):
            with self.subTest(value=value):
                with self.assertRaises(UnusableDecimalScale):
                    decode_decimal(value)
        # Positive control: the types that legitimately are numbers here.
        for value in ('12', 12, 12.0, decimal.Decimal(12)):
            with self.subTest(control=value):
                self.assertEqual(decimal.Decimal(12), decode_decimal(value))

    def test_a_bare_exponent_is_bounded_like_a_scale(self):
        # The dict path is bounded for a measured reason and this one was not.
        # 1E-999999999 is the same hazard MAX_SIGN_SCALE exists for, arriving
        # through a value that `is_finite()` waves through: it compares
        # neither == 0 nor <= 0, so a corrupt LeavesQuantity reads as still
        # outstanding forever -- while behaving as zero in arithmetic.
        stray = decimal.Decimal('1E-999999999')
        self.assertFalse(stray == 0)
        self.assertFalse(stray <= 0)
        self.assertEqual(decimal.Decimal(0), +stray)

        for label, value in (('1E-999999999', '1E-999999999'),
                             ('1E999999999', '1E999999999'),
                             ('1E-65', '1E-65'), ('1E65', '1E65'),
                             # Not `str()`-able within the digit limit, which
                             # is the point: formatting it is the operation
                             # being guarded against.
                             ('10**10000', 10 ** 10000),
                             ('-10**10000', -10 ** 10000)):
            with self.subTest(value=label):
                with self.assertRaises(UnusableDecimalScale):
                    decode_decimal(value)
        # Positive control: the bound is far outside anything real.
        for value in ('1E-64', '1E64', '7.35', '-7.35', 10 ** 20):
            with self.subTest(value=value):
                self.assertEqual(decimal.Decimal(value), decode_decimal(value))

    def test_a_wide_digit_string_does_not_escape_as_a_bare_value_error(self):
        # `int()` refuses a string past sys.get_int_max_str_digits() -- 4300
        # by default -- with a ValueError that is not a SchwabError, and the
        # range check cannot run until int() has returned. The value is
        # irrelevant: '0' * 4300 + '1' is the integer 1.
        for width in (4300, 4301, 100000):
            with self.subTest(width=width):
                self.assertEqual(
                        decimal.Decimal('0.000001'),
                        decode_decimal({'lo': '0' * width + '1',
                                        'signScale': 12}))
                self.assertEqual(
                        decimal.Decimal('0.000001'),
                        decode_decimal({'lo': '1',
                                        'signScale': '0' * width + '12'}))
        # A member that is genuinely long, rather than merely padded. The
        # cases above all strip to '1', so `lstrip` alone satisfies them and
        # the length check never runs -- exactly the shape that reads as a
        # working guard while proving nothing.
        #
        # The first pair is one digit past each width: thirty for a lone
        # `lo`, which may carry the whole 96-bit mantissa, and eleven for a
        # `mid`. An eleven-digit `lo` is an ordinary value. That pair is
        # refused by the range check, not the length check -- the length
        # check decides nothing short of `int()`'s own limit, which only the
        # wider pairs reach -- and the scale is refused by its 0..64 bound
        # before either.
        for lo_width, member_width in ((30, 11), (4301, 4301),
                                       (100000, 100000)):
            with self.subTest(nonzero_width=lo_width):
                with self.assertRaises(UnusableDecimalScale):
                    decode_decimal({'lo': '1' * lo_width, 'signScale': 12})
                with self.assertRaises(UnusableDecimalScale):
                    decode_decimal({'lo': '1',
                                    'signScale': '1' * member_width})
                with self.assertRaises(UnusableDecimalScale):
                    decode_decimal({'lo': '1', 'mid': '1' * member_width,
                                    'signScale': 12})

    def test_an_object_that_is_not_a_decimal_is_refused_not_read_as_zero(self):
        # The mantissa-less shortcut returns zero without inspecting
        # anything, so corruption that changes *keys* rather than values
        # reached it -- and zero is what a $0 commission and a completed fill
        # look like. Refused only when *none* of the four keys is present;
        # see the schema-addition test below for the other direction.
        for value in ({'Lo': 7350000, 'SignScale': 12},     # PascalCase
                      {'low': 7350000},                     # typo, no scale
                      {'symbol': 'AAPL', 'quantity': 100},  # another object
                      {'flags': 0}):
            with self.subTest(value=value):
                with self.assertRaises(UnusableDecimalScale):
                    decode_decimal(value)
        # Positive control: the two shapes that legitimately decode to zero.
        self.assertEqual(decimal.Decimal(0), decode_decimal({}))
        self.assertEqual(decimal.Decimal(0), decode_decimal({'signScale': 12}))

    def test_a_renamed_key_is_refused_in_either_direction(self):
        # An unknown key beside a *missing* component is ambiguous: the
        # unknown key may be that component under a new name. Both directions
        # are refused, and they are not symmetric in cost.
        #
        # A renamed mantissa read as zero. A renamed *scale* was far worse:
        # with no signScale the absent-scale rule applies, so a $7.35 limit
        # price decoded as $7,350,000 -- a confident wrong number six orders
        # of magnitude out, on the field this feed exists to carry. Both
        # halves are tested here.
        for value in ({'lo': '7350000', 'SignScale': 12},   # scale renamed
                      {'lo': '7350000', 'signscale': 12},   # ... or recased
                      {'lo': '7350000', 'Scale': 12},
                      {'Lo': '7350000', 'signScale': 12},   # mantissa renamed
                      {'low': 7350000, 'signScale': 12}):
            with self.subTest(value=value):
                util._reported_keys.clear()
                with self.assertRaises(UnusableDecimalScale):
                    decode_decimal(value)
        util._reported_keys.clear()

    def test_an_explicit_null_does_not_slip_the_rename_guard(self):
        """The guard and the decoder must mean the same thing by "present".

        The guard was keyed on the key being *there* while the mantissa loop
        and the scale twenty lines below treat an explicit null as an
        absence -- deliberately, with their own test. So a null walked
        straight back into the defect the guard was added to close: no usable
        `signScale`, the absent-scale rule applies, and a $7.35 limit price
        decodes as $7,350,000.
        """
        for src in ('{"lo": "7350000", "signScale": null, "SignScale": 12}',
                    '{"lo": "7350000", "signScale": null, "flags": 0}',
                    '{"lo": null, "signScale": 12, "low": 7350000}',
                    '{"lo": null, "mid": null, "hi": null,'
                    ' "signScale": 12, "f": 0}'):
            with self.subTest(src=src):
                util._reported_keys.clear()
                with self.assertRaises(UnusableDecimalScale):
                    decode_decimal(json.loads(src))
        # Positive control: a null in a slot is still an absence when no
        # unknown key is in play, which is the rule this borrows.
        util._reported_keys.clear()
        self.assertEqual(
                decimal.Decimal(15300),
                decode_decimal(json.loads(
                    '{"lo": "15300", "signScale": null}')))

    def test_any_unrecognised_key_refuses_the_object(self):
        """The whole rule, and it is the whole rule on purpose.

        A version that tried to decode around an unknown key was wrong three
        ways: it ignored one on an object that looked complete and
        refused one where a component was missing, and "missing" turned out
        to have more spellings than anyone enumerates -- absent, null, and
        zero, each a $7.35 limit price decoding as $7,350,000. The last of
        those cannot be closed, because `signScale: 0` genuinely means scale
        0. Measured against recorded traffic, the clever version also bought
        nothing: 27 of 37 objects still decoded wrong under a rename that
        keeps the old key at its default.
        """
        for value in (
                # A key alongside a complete object -- a schema addition.
                {'lo': '21480000', 'signScale': 12, 'flags': 0},
                {'lo': '705032704', 'mid': 1, 'signScale': 12, 'f': 0},
                {'lo': '7350000', 'signScale': 13, 'IsNeg': True},
                # A component missing, in each of its three spellings.
                {'lo': '7350000', 'SignScale': 12},
                {'lo': '7350000', 'signScale': None, 'SignScale': 12},
                {'lo': '7350000', 'signScale': 0, 'SignScale': 12},
                {'lo': '7350000', 'signScale': '0', 'SignScale': 12},
                # A renamed member of the mantissa, which the previous
                # version documented as an inherent gap. It is not one.
                {'lo': '705032704', 'Mid': 1, 'signScale': 12},
                # An object that is not one of these at all.
                {'symbol': 'AAPL', 'quantity': 100}):
            with self.subTest(value=value):
                util._reported_keys.clear()
                with self.assertRaises(UnusableDecimalScale):
                    decode_decimal(value)
        util._reported_keys.clear()

    def test_every_real_payload_still_decodes(self):
        # The control on the rule above: refusing everything would satisfy
        # it. All but the last are shapes a live account actually sends,
        # including the two that omit a component, which is what made the
        # clever version look necessary. The non-zero `mid` is reasoned from
        # the .NET layout rather than observed -- no captured payload has
        # carried one, which the streaming docs mark as unconfirmed.
        for value, expected in (({'lo': '21480000', 'signScale': 12}, '21.48'),
                                ({'lo': '15300'}, '15300'),
                                ({'signScale': 12}, '0'),
                                ({}, '0'),
                                ({'lo': '7350000', 'signScale': 13}, '-7.35'),
                                # The measured shape of a fill principal
                                # over 32 bits; the value is invented.
                                ({'lo': '7350000000', 'signScale': 12},
                                 '7350'),
                                ({'lo': '705032704', 'mid': 1,
                                  'signScale': 12}, '5000')):
            with self.subTest(value=value):
                self.assertEqual(decimal.Decimal(expected),
                                 decode_decimal(value))

    def test_the_refusal_says_it_is_a_schema_change_not_corruption(self):
        # Two messages, because they are different events for whoever reads
        # them: the venue changing under everyone, and a caller passing the
        # wrong object.
        with self.assertRaises(UnusableDecimalScale) as caught:
            decode_decimal({'lo': '1', 'signScale': 12, 'flags': 0})
        self.assertIn('does not know', str(caught.exception))
        with self.assertRaises(UnusableDecimalScale) as caught:
            decode_decimal({'symbol': 'AAPL'})
        self.assertIn('not a decimal object', str(caught.exception))
        util._reported_keys.clear()

    def test_an_added_key_is_still_reported_once_per_key(self):
        # The refusal names the key on every field of every message. The log
        # line is the once-per-key operator signal that says this is new
        # rather than broken, and it is what a bug report carries.
        util._reported_keys.clear()
        with self.assertLogs(util.get_logger(), level='WARNING') as caught:
            for i in range(3):
                with self.assertRaises(UnusableDecimalScale):
                    decode_decimal({'lo': str(i), 'signScale': 12, 'flags': 0})
        self.assertEqual(1, len(caught.output))
        self.assertIn("'flags'", caught.output[0])

        # A *second* distinct key earns its own line. Without this, "report
        # the first key ever seen and then go permanently silent" passes the
        # whole suite -- and the report is the entire thing this behaviour
        # adds over refusing quietly.
        with self.assertLogs(util.get_logger(), level='WARNING') as caught:
            with self.assertRaises(UnusableDecimalScale):
                decode_decimal({'lo': '1', 'signScale': 12, 'scale': 6})
        self.assertEqual(1, len(caught.output))
        self.assertIn("'scale'", caught.output[0])
        self.assertNotIn("'flags'", caught.output[0])
        util._reported_keys.clear()

    def test_the_wrong_object_does_not_consume_the_report_budget(self):
        """The report is bounded, module-level and never cleared.

        `decode_decimal`'s own docstring names "any consumer decoding fields
        generically", and such a caller hands it every nested object it
        walks. Reported from the not-a-decimal-object branch as well, one
        real ACCT_ACTIVITY frame filled all 32 slots with `AccountNumber`,
        `BaseEvent`, `EventType` and the like -- after which a genuine schema
        addition logged nothing at all, for the life of the process.
        """
        util._reported_keys.clear()
        with self.assertNoLogs(util.get_logger(), level='WARNING'):
            for value in ({'AccountNumber': '1', 'BaseEvent': {}},
                          {'EventType': 'OrderCreated'},
                          {'symbol': 'AAPL', 'quantity': 100}):
                with self.assertRaises(UnusableDecimalScale):
                    decode_decimal(value)
        self.assertEqual(set(), util._reported_keys)
        # Positive control: the schema-change branch still reports, so the
        # silence above is not silence everywhere.
        with self.assertLogs(util.get_logger(), level='WARNING'):
            with self.assertRaises(UnusableDecimalScale):
                decode_decimal({'lo': '1', 'signScale': 12, 'brandNew': 1})
        util._reported_keys.clear()

    def test_the_unknown_key_report_is_bounded(self):
        # The set never shrinks and what goes into it is the venue's to
        # choose, so it is capped -- in count, and per key in length.
        util._reported_keys.clear()
        for i in range(util._MAX_REPORTED_KEYS * 3):
            with self.assertRaises(UnusableDecimalScale):
                decode_decimal({'lo': '1', 'signScale': 12, 'k%d' % i: 0})
        self.assertLessEqual(len(util._reported_keys),
                             util._MAX_REPORTED_KEYS)

        # One object carrying more keys than the cap: a fixture that adds one
        # per call is sized to the guard rather than to the input the guard's
        # own comment names.
        util._reported_keys.clear()
        crowded = {'lo': '1', 'signScale': 12}
        crowded.update({'k%d' % i: 0
                        for i in range(util._MAX_REPORTED_KEYS * 20)})
        with self.assertRaises(UnusableDecimalScale):
            decode_decimal(crowded)
        self.assertLessEqual(len(util._reported_keys),
                             util._MAX_REPORTED_KEYS)
        # Positive control. `assertLessEqual(len(...), 32)` holds when the
        # cap works *and* when the report never ran at all, and nothing else
        # in this test distinguishes them.
        self.assertEqual(util._MAX_REPORTED_KEYS, len(util._reported_keys))
        util._reported_keys.clear()

    def test_a_key_that_cannot_be_named_does_not_escape(self):
        # A key is whatever the decoder produced. `str()` of a wide integer
        # raises past sys.get_int_max_str_digits(), and both the report and
        # the refusal message name the unknown keys.
        util._reported_keys.clear()
        with self.assertRaises(UnusableDecimalScale) as caught:
            decode_decimal({'lo': '1', 'signScale': 12, 10 ** 6000: 0})
        self.assertIn('cannot be named', str(caught.exception))
        # Bounded in length, not only in count: the set never shrinks.
        util._reported_keys.clear()
        with self.assertRaises(UnusableDecimalScale):
            decode_decimal({'lo': '1', 'signScale': 12, 'k' * 5000: 0})
        self.assertLessEqual(max(len(k) for k in util._reported_keys), 64)
        util._reported_keys.clear()

    def test_a_refusal_message_cannot_raise_on_the_keys_it_names(self):
        """A case per call site, not per helper.

        `_safe_key` has three callers -- the report, and the two refusal
        messages -- and a red-proof case on the helper's own body proves only
        that *some* caller reaches it. Reverting both refusal sites to
        `str()` left the entire suite green, because every existing fixture
        took the ignored path.
        """
        # The ambiguity refusal: a missing component, and an unnameable key.
        with self.assertRaises(UnusableDecimalScale) as caught:
            decode_decimal({'lo': '1', 10 ** 6000: 0})
        self.assertIn('cannot be named', str(caught.exception))
        # The not-a-decimal-object refusal: no known key at all.
        with self.assertRaises(UnusableDecimalScale) as caught:
            decode_decimal({10 ** 6000: 0})
        self.assertIn('cannot be named', str(caught.exception))
        # Positive control: an ordinary key is named, so the assertions above
        # are not passing on a message that says nothing.
        with self.assertRaises(UnusableDecimalScale) as caught:
            decode_decimal({'lo': '1', 'Scale': 12})
        self.assertIn('Scale', str(caught.exception))

    def test_a_refusal_message_is_bounded_in_count_as_well(self):
        # `_safe_key` caps each name and the join did not, so an object
        # carrying 100,000 unknown keys produced a 2.9 MB exception message.
        crowded = {'lo': '1'}
        crowded.update({'k%05d' % i: 0 for i in range(100000)})
        with self.assertRaises(UnusableDecimalScale) as caught:
            decode_decimal(crowded)
        self.assertLess(len(str(caught.exception)), 1000)
        self.assertIn('more', str(caught.exception))

    def test_an_empty_object_is_the_omit_everything_zero(self):
        # The `:raises:` block said an object carrying none of the four is
        # refused, which reads as covering `{}`. It does not and should not:
        # the guard lives behind "an unrecognised key is present", and `{}`
        # is the omit-everything spelling of zero.
        self.assertEqual(decimal.Decimal(0), decode_decimal({}))
        with self.assertRaises(UnusableDecimalScale):
            decode_decimal({'Foo': 1})

    def test_a_dict_subclass_whose_get_and_getitem_disagree_is_catchable(self):
        # Read as `value['signScale']`, the scale would escape as a KeyError
        # from a mapping whose `get` and `__getitem__` disagree. Fields are
        # read once through the mapping's `items()`, so a `get` that
        # invents a key is not consulted at all: this one lists no
        # `signScale`, and decodes at scale 0 like any object without one.
        class Odd(dict):
            def get(self, key, default=None):
                return 12 if key == 'signScale' else super().get(key, default)

        odd = Odd(lo='7350000')
        with self.assertRaises(KeyError):       # the shape being guarded
            odd['signScale']
        self.assertEqual(decimal.Decimal('7350000'), decode_decimal(odd))
        # Positive control: the same mapping storing the scale decodes as it.
        self.assertEqual(decimal.Decimal('7.35'),
                         decode_decimal(Odd(lo='7350000', signScale=12)))

    def test_a_corrupt_scale_is_refused_even_with_no_mantissa_to_scale(self):
        # The mantissa-less shortcut returned before the scale was looked at,
        # so `{"signScale": "garbage"}` decoded as a clean zero while the
        # identical corruption one key over raised. No wrong number -- a
        # mantissa-less object is zero at any scale -- but it discarded the
        # only signal that the message was corrupt, on a feed where zero is
        # what a $0 commission and a completed fill look like.
        for corrupt in ('garbage', 1000000000, 1.9, -1, [1], 65, True, ''):
            with self.subTest(signScale=corrupt):
                with self.assertRaises(UnusableDecimalScale):
                    decode_decimal({'signScale': corrupt})
                # The same corruption one key over, which always raised.
                with self.assertRaises(UnusableDecimalScale):
                    decode_decimal({'lo': '1', 'signScale': corrupt})
        # Positive control: a sound scale with nothing to scale is still zero.
        for sound in (0, 12, 13, 64, '12'):
            with self.subTest(control=sound):
                self.assertEqual(decimal.Decimal(0),
                                 decode_decimal({'signScale': sound}))

    def test_a_mantissa_less_object_is_zero_without_a_negative_sign(self):
        # What the shortcut still does now that the scale above it is
        # validated either way: falling through would give Decimal('0.000000')
        # at scale 12 and Decimal('-0.000000') at 13. Both compare equal to
        # zero, and the second prints as a negative quantity -- on the field
        # this is most often read for, LeavesQuantity on a completed fill.
        for scale in (12, 13):
            with self.subTest(signScale=scale):
                self.assertEqual(
                        '0', str(decode_decimal({'signScale': scale})))

    def test_an_explicit_null_is_an_absence_in_every_slot(self):
        # Schwab omits rather than nulls, so this is reasoned from the rule
        # the members already follow rather than observed. It is here because
        # the two halves briefly disagreed: `{"lo": null}` was an absence and
        # `{"signScale": null}` was corruption.
        self.assertEqual(
                decimal.Decimal('0.000001'),
                decode_decimal(json.loads(
                    '{"lo": "1", "mid": null, "hi": null, "signScale": 12}')))
        self.assertEqual(
                decimal.Decimal('15300'),
                decode_decimal(json.loads(
                    '{"lo": "15300", "signScale": null}')))
        self.assertEqual(
                decimal.Decimal(0),
                decode_decimal(json.loads('{"lo": null, "signScale": null}')))

    def test_an_error_message_cannot_raise_on_what_it_describes(self):
        # `repr` of a dict holding a 5,000-digit int raises ValueError, which
        # would replace a SchwabError describing corruption with a bare
        # exception describing nothing -- the escape class this function
        # closes everywhere else, arriving through the error path.
        for label, value in (('wide lo', {'lo': 10 ** 5000}),
                             ('wide signScale',
                              {'lo': '1', 'signScale': 10 ** 5000}),
                             ('both', {'lo': 10 ** 5000,
                                       'signScale': 10 ** 5000})):
            with self.subTest(label):
                with self.assertRaises(UnusableDecimalScale) as caught:
                    decode_decimal(value)
                # The message is produced, and is not the whole integer.
                self.assertLess(len(str(caught.exception)), 400)
        with self.assertRaises(UnusableDecimalScale) as caught:
            decode_decimal(10 ** 5000)
        self.assertLess(len(str(caught.exception)), 400)


from schwaby.contrib.util import UnparsableMessageData, parse_message_data


class HeuristicJsonDecoderBytesTest(unittest.TestCase):

    @no_duplicates
    def test_a_bytes_frame_that_is_not_json_raises_the_decode_error(self):
        # Its repair step replaced with str arguments, which raised TypeError
        # on bytes -- an exception the client does not report as a frame that
        # would not parse, so the receive loop ended with nothing said.
        from schwaby.contrib.util import HeuristicJsonDecoder
        with self.assertRaises(json.decoder.JSONDecodeError):
            HeuristicJsonDecoder().decode_json_string(b'not json')
        # Positive control: a str frame still decodes.
        self.assertEqual({'a': '\\'}, HeuristicJsonDecoder().decode_json_string(
                '{"a": "\\\\"}'))


class ParseMessageDataTest(unittest.TestCase):

    @no_duplicates
    def test_a_json_object_is_parsed(self):
        self.assertEqual({'EventType': 'OrderCreated'},
                         parse_message_data('{"EventType": "OrderCreated"}'))
        self.assertEqual({'a': 1}, parse_message_data('  \n{"a": 1}\n '))

    @no_duplicates
    def test_the_subscribed_ack_is_none(self):
        for value in ('', '   ', '\n\t'):
            with self.subTest(value=value):
                self.assertIsNone(parse_message_data(value))

    @no_duplicates
    def test_a_notice_is_returned_as_its_text_and_never_raises(self):
        for value in ('Feature not supported', '[1, 2]', '"quoted"', '7',
                      'notice with a { later on'):
            with self.subTest(value=value):
                self.assertEqual(value, parse_message_data(value))
                self.assertIs(str, type(parse_message_data(value)))

    @no_duplicates
    def test_a_dict_is_returned_unchanged(self):
        parsed = {'EventType': 'OrderCreated'}
        self.assertIs(parsed, parse_message_data(parsed))
        once = parse_message_data('{"a": 1}')
        self.assertEqual(once, parse_message_data(once))

    @no_duplicates
    def test_text_that_starts_like_an_object_but_is_not_one_raises(self):
        deep = '{"a":' * 200000 + '1' + '}' * 200000
        for value in ('{', '{"a": 1', '{"a": 1} trailing', '  {nope}',
                      '{"AccountNumber": "12345678"', deep):
            with self.subTest(value=value[:40]):
                with self.assertRaises(UnparsableMessageData) as cm:
                    parse_message_data(value)
                self.assertIsInstance(cm.exception, SchwabError)
                self.assertIsInstance(cm.exception, ValueError)
                self.assertNotIn('12345678', str(cm.exception))

    @no_duplicates
    def test_invisible_characters_before_an_object_do_not_make_a_notice(self):
        for prefix in ('\ufeff', '\u200b', '\u2060', '\u200e', '\x00',
                       '\ufeff\u3000', '\ufeff\u2028', '\ufeff\u2029'):
            with self.subTest(prefix=prefix):
                self.assertEqual({'a': 1},
                                 parse_message_data(prefix + '{"a": 1}'))
                with self.assertRaises(UnparsableMessageData):
                    parse_message_data(prefix + '{"a": 1')

    @no_duplicates
    def test_whitespace_json_does_not_accept_is_still_stripped(self):
        # str.strip removes U+2028 and U+00A0; JSON does not accept either.
        for prefix in ('\u2028', '\u00a0'):
            with self.subTest(prefix=prefix):
                self.assertEqual({'a': 1},
                                 parse_message_data(prefix + '{"a": 1}'))

    @no_duplicates
    def test_a_notice_keeps_its_surrounding_whitespace(self):
        self.assertEqual('  Feature not supported\n',
                         parse_message_data('  Feature not supported\n'))

    @no_duplicates
    def test_a_str_subclass_is_read_as_its_text(self):
        class Text(str):
            def strip(self, *args):
                return ''

        self.assertEqual('Feature not supported',
                         parse_message_data(Text('Feature not supported')))

    @no_duplicates
    def test_anything_else_is_refused(self):
        class FakeDict:
            __class__ = dict

        class FakeStr:
            __class__ = str

        for value in (None, b'{"a": 1}', 7, ['{"a": 1}'], FakeDict(),
                      FakeStr()):
            with self.subTest(value=value):
                with self.assertRaisesRegex(TypeError,
                                            'must be a str or a dict'):
                    parse_message_data(value)
