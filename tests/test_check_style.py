"""The house layout check.

Most of what it checks is characters, and is covered by running it over
the tree. What needs a test of its own is the one rule that reads the
syntax tree: a register painted with a default in a clocked process and
then overridden.
"""
import os
import tempfile
import textwrap
import unittest

from isomorph.check_style import check


def problems (source):
    directory = tempfile.mkdtemp(prefix = 'iso_style_')
    path = os.path.join(directory, 'unit.py')
    with open(path, 'w', encoding = 'ascii') as handle:
        handle.write(textwrap.dedent(source))
    found = check(path)
    os.remove(path)
    os.rmdir(directory)
    return [text for _, text in found]


class PaintedDefaultTests (unittest.TestCase):
    def test_a_painted_default_is_reported (self):
        found = problems('''\
            @always_ff (i_clock.posedge)
            def unit_logic ():
                o_strobe.next = False
                if (i_go):
                    o_strobe.next = True
        ''')
        self.assertEqual(len(found), 1)
        self.assertIn('o_strobe', found[0])
        self.assertIn('assign it once', found[0])

    def test_a_strobe_from_its_condition_is_not (self):
        self.assertEqual(problems('''\
            @always_ff (i_clock.posedge)
            def unit_logic ():
                o_strobe.next = i_go
        '''), [])

    def test_both_arms_of_an_if_else_are_not (self):
        self.assertEqual(problems('''\
            @always_ff (i_clock.posedge)
            def unit_logic ():
                if (i_go):
                    o_strobe.next = True
                else:
                    o_strobe.next = False
        '''), [])

    def test_a_comb_process_is_left_alone (self):
        # a default and an override is how a mux is written in comb
        self.assertEqual(problems('''\
            @always_comb
            def unit_comb ():
                o_pick.next = i_a
                if (i_sel):
                    o_pick.next = i_b
        '''), [])

    def test_it_reaches_inside_a_match_arm (self):
        found = problems('''\
            @always_ff (i_clock.posedge)
            def unit_logic ():
                match fsm:
                    case state.IDLE:
                        o_send.next = False
                        if (i_go):
                            o_send.next = True
        ''')
        self.assertEqual(len(found), 1)
        self.assertIn('o_send', found[0])

    def test_holding_a_register_is_not_a_default (self):
        # nothing assigned on the other path: the register holds, which
        # is the house idiom, not a painted default
        self.assertEqual(problems('''\
            @always_ff (i_clock.posedge)
            def unit_logic ():
                if (i_load):
                    o_data.next = i_value
        '''), [])

    def test_a_file_that_will_not_parse_is_left_to_python (self):
        self.assertEqual(problems('def broken(\n'), [])


if __name__ == '__main__':
    unittest.main()
