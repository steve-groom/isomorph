"""Reserved identifiers for every output.

A Python name is the netlist (or C field) name in
SystemVerilog, VHDL and the C99 smoke. Convert checks all of them on
every run, even if only one output is requested. A clash is an error,
not a silent rename.
"""
import re


# IEEE 1076 reserved words; VHDL matching is case-insensitive.
VHDL_RESERVED = set('''
    abs access after alias all and architecture array assert attribute
    begin block body buffer bus case component configuration constant
    context default disconnect downto else elsif end entity exit file
    for force function generate generic group guarded if impure in
    inertial inout is label library linkage literal loop map mod nand
    new next nor not null of on open or others out package parameter
    port postponed procedure process pure range record register reject
    release rem report return rol ror select severity signal shared sla
    sll sra srl subtype then to transport type unaffected units until
    use variable wait when while with xnor xor
'''.split())

# IEEE 1800-2017 keywords that tools reject as identifiers.
SV_RESERVED = set('''
    accept_on alias always always_comb always_ff always_latch and assert
    assign assume automatic before begin bind bins binsof bit break buf
    bufif0 bufif1 byte case casex casez cell chandle checker class clocking
    cmos config const constraint context continue cover covergroup
    coverpoint cross deassign default defparam design disable dist do edge
    else end endcase endchecker endclass endclocking endconfig endfunction
    endgenerate endgroup endinterface endmodule endpackage endprimitive
    endprogram endproperty endspecify endsequence endtable endtask enum
    event eventually expect export extends extern final first_match for
    force foreach forever fork forkjoin function generate genvar global
    highz0 highz1 if iff ifnone ignore_bins illegal_bins implements
    implies import incdir include initial inout input inside instance int
    integer interconnect interface intersect join join_any join_none large
    let liblist library local localparam logic longint macromodule matches
    medium modport module nand negedge nettype new nexttime nmos nor
    noshowcancelled not notif0 notif1 null or output package packed
    parameter pmos posedge primitive priority program property protected
    pull0 pull1 pulldown pullup pulsestyle_ondetect pulsestyle_onevent
    pure rand randc randcase randsequence rcmos real realtime ref reg
    reject_on release repeat restrict return rnmos rpmos rtran rtranif0
    rtranif1 s_always s_eventually s_nexttime s_until s_until_with
    scalared sequence shortint shortreal showcancelled signed small solve
    specify specparam static string strong strong0 strong1 struct super
    supply0 supply1 sync_accept_on sync_reject_on table tagged task this
    throughout time timeprecision timeunit tran tranif0 tranif1 tri tri0
    tri1 triand trior trireg type typedef union unique unique0 unsigned
    until until_with untyped use uwire var vectored virtual void wait
    wait_order wand weak weak0 weak1 while wildcard wire with within wor
    xnor xor
'''.split())

# C99 keywords (ISO/IEC 9899:1999). Generated .c must compile.
C99_RESERVED = set('''
    auto break case char const continue default do double else enum
    extern float for goto if inline int long register restrict return
    short signed sizeof static struct switch typedef union unsigned
    void volatile while _Bool _Complex _Imaginary
'''.split())


IDENTIFIER = re.compile(r'[A-Za-z][A-Za-z0-9_]*')


def clash_message (name):
    """Error text if name is reserved or not an identifier in every
    output, else None."""
    if not name:
        return None
    base = name.split('[')[0]
    if (not IDENTIFIER.fullmatch(base) or '__' in base
            or base.endswith('_')):
        # VHDL: a letter first, no two underscores together, none at
        # the end. The C99 smoke also keeps the double underscore for
        # itself, as the shadow a register's next value sits in
        return (f'{base} is not a legal VHDL identifier: a name starts '
                'with a letter and has no double or trailing underscore; '
                'isomorph names must be legal in SystemVerilog, VHDL and '
                'C99')
    if base.lower() in VHDL_RESERVED:
        return (f'{base} is a VHDL reserved word; isomorph names must '
                'be legal in SystemVerilog, VHDL and C99')
    if base in SV_RESERVED:
        return (f'{base} is a SystemVerilog reserved word; isomorph '
                'names must be legal in SystemVerilog, VHDL and C99')
    if base in C99_RESERVED:
        return (f'{base} is a C99 reserved word; isomorph names must '
                'be legal in SystemVerilog, VHDL and C99')
    return None
