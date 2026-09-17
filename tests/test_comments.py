"""Comments travel. Every comment written in the Python
appears at the same place in the SystemVerilog and the VHDL."""
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest

DESIGN = textwrap.dedent('''\
    """Module docstring, used when the block has none.

    Second paragraph of it.
    """
    from isomorph import (block, signal, always_ff, always_comb,
                          assign, attr, instances, main)


    @block
    def probe (
            i_clock,
            # above a port
            i_sel,          # trailing on a port
            o_q,            # another port comment
            o_r
        ):
        # above a signal declaration
        state = signal(4)                   # trailing on a signal
        parity = signal()
        attr(state, keep = True)            # trailing on an attr() line

        # ---------------------------------------------------------
        # a section bar above a process
        # ---------------------------------------------------------
        @always_comb
        def parity_comb ():
            # A: above the first statement of a suite
            parity.next = state[3] ^ state[2] ^ state[1] ^ state[0]
            # B: after the last statement of a suite

        @always_ff (i_clock.posedge)
        def fsm ():
            if (i_sel == 0):
                state.next = 1              # trailing inside an if
            # C: above an elif
            elif (i_sel == 1):
                # D: first statement of an elif suite
                state.next = 2
            # E: above the else
            else:
                state.next = 3
                # F: after the last statement of an else suite

        # above a continuous assignment
        assign(o_q, lambda: parity)         # trailing on the assign
        assign(o_r, lambda: state[0])

        return instances()


    def elaborate_probe ():
        return probe(i_clock = signal(), i_sel = signal(4),
                     o_q = signal(), o_r = signal())


    if (__name__ == '__main__'):
        import sys
        sys.exit(main(elaborate_probe))
''')

POSITIONS = ['A: above the first statement of a suite',
             'B: after the last statement of a suite',
             'C: above an elif',
             'D: first statement of an elif suite',
             'E: above the else',
             'F: after the last statement of an else suite',
             'above a signal declaration',
             'trailing on a signal',
             'a section bar above a process',
             'trailing inside an if',
             'above a continuous assignment',
             'trailing on the assign',
             'above a port',
             'trailing on a port',
             'another port comment',
             'trailing on an attr() line']


class CommentTests (unittest.TestCase):
    @classmethod
    def setUpClass (cls):
        cls.dir = tempfile.mkdtemp(prefix = 'iso_cmt_')
        path = os.path.join(cls.dir, 'probe.py')
        with open(path, 'w') as f:
            f.write(DESIGN)
        result = subprocess.run(
            [sys.executable, path, '--sv', '--vhdl', '--lint'],
            capture_output = True, text = True, cwd = cls.dir)
        if result.returncode != 0:
            raise AssertionError(result.stderr or result.stdout)
        # a file per module, in a directory named for the design
        build = os.path.join(cls.dir, 'build', 'probe')
        cls.sv = open(os.path.join(build, 'probe.sv')).read()
        cls.vhdl = open(os.path.join(build, 'probe.vhd')).read()

    def test_every_position_reaches_systemverilog (self):
        for text in POSITIONS:
            with self.subTest(text):
                self.assertIn(text, self.sv)

    def test_every_position_reaches_vhdl (self):
        for text in POSITIONS:
            with self.subTest(text):
                self.assertIn(text, self.vhdl)

    def test_hash_becomes_the_language_comment (self):
        self.assertIn('// A: above the first', self.sv)
        self.assertIn('-- A: above the first', self.vhdl)
        self.assertNotIn('#', self.sv)

    def test_port_comments_sit_with_their_port (self):
        for text in (self.sv, self.vhdl):
            lines = text.splitlines()
            i = next(n for n, l in enumerate(lines)
                     if 'trailing on a port' in l)
            self.assertIn('i_sel', lines[i])
            a = next(n for n, l in enumerate(lines) if 'above a port' in l)
            self.assertIn('i_sel', lines[a + 1])

    def test_module_docstring_is_the_header (self):
        self.assertIn('Module docstring, used when the block has none.',
                      self.sv)
        self.assertIn('Second paragraph of it.', self.sv)
        # a bar separates design units, then the header comment
        first = [line for line in self.sv.splitlines()
                 if line.strip() and set(line.strip()) != {'/', '-'}][0]
        self.assertTrue(first.startswith('// Module'), first)
        self.assertNotIn('/*', self.sv)
        self.assertIn('Module docstring, used when the block has none.',
                      self.vhdl)

    def test_branch_comments_sit_above_their_keyword (self):
        sv = self.sv.splitlines()
        c = next(i for i, l in enumerate(sv) if 'C: above an elif' in l)
        self.assertIn('else if', sv[c + 1])
        self.assertIn('end', sv[c - 1])
        e = next(i for i, l in enumerate(sv) if 'E: above the else' in l)
        self.assertIn('else begin', sv[e + 1])

        vhdl = self.vhdl.splitlines()
        c = next(i for i, l in enumerate(vhdl) if 'C: above an elif' in l)
        self.assertIn('elsif', vhdl[c + 1])
        e = next(i for i, l in enumerate(vhdl) if 'E: above the else' in l)
        self.assertEqual(vhdl[e + 1].strip(), 'else')

    def test_suite_tail_stays_inside_its_suite (self):
        sv = self.sv.splitlines()
        b = next(i for i, l in enumerate(sv)
                 if 'B: after the last statement' in l)
        self.assertEqual(sv[b + 1].strip(), 'end',
                         'the tail comment must stay inside parity_comb')
        f = next(i for i, l in enumerate(sv)
                 if 'F: after the last statement' in l)
        self.assertEqual(sv[f + 1].strip(), 'end')

    def test_no_comment_is_emitted_twice (self):
        for text in POSITIONS:
            with self.subTest(text):
                self.assertEqual(self.sv.count(text), 1)
                self.assertEqual(self.vhdl.count(text), 1)


if __name__ == '__main__':
    unittest.main()


HIERARCHY = textwrap.dedent('''\
    """A parent with a long comment block above each of its children."""
    from isomorph import (block, signal, always_ff, instances, main)


    @block
    def leaf (i_clock, i_d, o_q):
        @always_ff (i_clock.posedge)
        def hold_logic ():
            o_q.next = i_d
        return instances()


    @block
    def parent (i_clock, i_d, o_q, WIDTH = 3):
        middle = signal(WIDTH)

        # L1: the first line of a long block
        # L2: the second line
        # L3: the third line
        # L4: the fourth line
        # L5: the line just above the instance
        inst_first = leaf(i_clock = i_clock, i_d = i_d, o_q = middle)

        # M1: one line above the second instance
        inst_second = leaf(i_clock = i_clock, i_d = middle, o_q = o_q)

        return instances()


    def elaborate_parent ():
        return parent(i_clock = signal(), i_d = signal(3),
                      o_q = signal(3))


    if (__name__ == '__main__'):
        import sys
        sys.exit(main(elaborate_parent))
''')


class InstanceCommentTests (unittest.TestCase):
    """A comment block above an instance travels whole.

    It used to be read through a fixed three line window, so a block
    longer than that lost its top, which is where the section bar and
    the first sentence live."""

    @classmethod
    def setUpClass (cls):
        cls.dir = tempfile.mkdtemp(prefix = 'iso_inst_')
        path = os.path.join(cls.dir, 'parent.py')
        with open(path, 'w') as f:
            f.write(HIERARCHY)
        result = subprocess.run(
            [sys.executable, path, '--sv', '--vhdl', '--lint'],
            capture_output = True, text = True, cwd = cls.dir)
        if result.returncode != 0:
            raise AssertionError(result.stderr or result.stdout)
        build = os.path.join(cls.dir, 'build', 'parent')
        cls.sv = open(os.path.join(build, 'parent.sv')).read()
        cls.vhdl = open(os.path.join(build, 'parent.vhd')).read()

    def test_the_whole_block_reaches_both_languages (self):
        for line in ('L1: the first line of a long block',
                     'L2: the second line',
                     'L3: the third line',
                     'L4: the fourth line',
                     'L5: the line just above the instance',
                     'M1: one line above the second instance'):
            with self.subTest(line):
                self.assertIn('// ' + line, self.sv)
                self.assertIn('-- ' + line, self.vhdl)

    def test_each_block_stays_with_its_own_instance (self):
        first = self.sv.index('inst_first')
        second = self.sv.index('inst_second')
        self.assertLess(self.sv.index('L1:'), first)
        self.assertLess(first, self.sv.index('M1:'))
        self.assertLess(self.sv.index('M1:'), second)


class ParameterOrderTests (unittest.TestCase):
    """A parameter a port width names is declared before the ports.

    SystemVerilog needs the name before its use, so it goes in the
    parameter port list. Declaring it in the body after the port list
    is what MyHDL emitted; Verilator accepts it, Quartus and Vivado do
    not."""

    DESIGN = textwrap.dedent('''\
        from isomorph import (block, signal, always_comb, instances, main)


        @block
        def widen (i_a, o_b, WIDTH = 5):
            @always_comb
            def widen_comb ():
                o_b.next = i_a
            return instances()


        def elaborate_widen ():
            return widen(i_a = signal(5), o_b = signal(5))


        if (__name__ == '__main__'):
            import sys
            sys.exit(main(elaborate_widen))
    ''')

    @classmethod
    def setUpClass (cls):
        cls.dir = tempfile.mkdtemp(prefix = 'iso_param_')
        path = os.path.join(cls.dir, 'widen.py')
        with open(path, 'w') as f:
            f.write(cls.DESIGN)
        result = subprocess.run(
            [sys.executable, path, '--sv', '--vhdl', '--lint'],
            capture_output = True, text = True, cwd = cls.dir)
        if result.returncode != 0:
            raise AssertionError(result.stderr or result.stdout)
        cls.sv = open(os.path.join(cls.dir, 'build', 'widen',
                                   'widen.sv')).read()

    def test_parameter_is_declared_before_the_ports_that_use_it (self):
        self.assertIn('module widen #(', self.sv)
        self.assertIn('parameter WIDTH = 5', self.sv)
        self.assertLess(self.sv.index('parameter WIDTH'),
                        self.sv.index('input  logic [WIDTH-1:0] i_a'))

    def test_it_is_not_also_declared_in_the_body (self):
        self.assertNotIn('parameter WIDTH = 5;', self.sv)
